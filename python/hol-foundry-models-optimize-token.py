#!/usr/bin/env python3

import argparse
import time

from openai import OpenAI
from pydantic import BaseModel

import identity

BANNER = "=" * 72

# Long enough to land above the 1,024 token threshold caching needs.
NUMBERS = "\n".join(str(n) for n in range(1000))

# A hit needs the first 1,024 tokens to be identical, so each demo opens with its own
# line and writes its own cache entry. Sharing one prompt would let the first demo warm
# the cache for the rest, and every demo would report a hit no matter what it set.
CACHE_PROMPTS = {
    "caching": "입력되는 숫자에 이어서 100개를 더 출력해줘.",
    "cache-retention": "내가 보여주는 입력되는 숫자에 이어서 100개를 더 출력해줘.",
    "cache-key": "아래에 나열한 숫자에 이어서 100개를 더 출력해줘.",
}

INTENT_MESSAGES = [
    {"role": "system", "content": "너는 대화 의도 분석을 전문으로 하는 AI야."},
    {"role": "user", "content": "Alice와 Bob이 금요일에 과학 박람회에 가기로 했어."},
]


class IntentEvent(BaseModel):
    intent: str


def parse_args():
    parser = argparse.ArgumentParser(description="Run the token optimization demos from 001-model-003-token-optimization.ipynb.")
    parser.add_argument("--endpoint", required=True, help="Foundry account endpoint")
    parser.add_argument("--deployment", default="gpt-5.4", help="model deployment name")

    identity.add_auth_arguments(parser)

    parser.add_argument(
        "--demo",
        choices=["caching", "cache-retention", "cache-key", "structured", "all"],
        default="all",
    )
    parser.add_argument("--rounds", type=int, default=10, help="how many times to repeat a caching call")
    parser.add_argument("--cache-key", default="prompt-cache-key-1", help="key that pins requests to one endpoint")
    parser.add_argument(
        "--structured-deployments",
        nargs="+",
        default=["gpt-4.1", "gpt-5.4"],
        help="deployments to compare on the same structured output",
    )
    return parser.parse_args()


def create_client(args):
    # v1 API: the stock OpenAI client, no AzureOpenAI and no api-version.
    # A callable api_key is the token provider, which the client refreshes per request.
    if args.auth == "api-key":
        api_key = args.api_key
    elif args.auth == "access-token":
        api_key = args.access_token
    else:
        api_key = identity.get_token_provider(args)
    return OpenAI(base_url=args.endpoint.rstrip("/") + "/openai/v1/", api_key=api_key)


def repeat_cached_call(client, args, demo, **cache_options):
    """cached_tokens is 0 on the first call and climbs once the prompt is cached."""
    opening = CACHE_PROMPTS[demo]
    print(f"[prompt] {opening}")
    for round_number in range(1, args.rounds + 1):
        started = time.time()
        response = client.chat.completions.create(
            model=args.deployment,
            messages=[{"role": "user", "content": f"{opening}\n{NUMBERS}"}],
            **cache_options,
        )
        details = response.usage.prompt_tokens_details
        print(f"[round {round_number:>2}/{args.rounds}] {details.model_dump()} ({time.time() - started:.2f}s)")


def caching(client, args):
    """Implicit caching: in-memory on one serving endpoint. Cleared after 5-10 minutes of
    inactivity, and always within an hour of its last use."""
    repeat_cached_call(client, args, "caching")


def cache_retention(client, args):
    """Extended cache: key/value tensors offload to GPU storage, kept up to 24 hours.
    Same cache, longer life — a hit still reports as plain cached_tokens."""
    repeat_cached_call(client, args, "cache-retention", prompt_cache_retention="24h")


def cache_key(client, args):
    """A deployment spreads over several serving endpoints, and the cache lives on one of
    them. The key routes matching requests to the same endpoint, so misses get rarer."""
    repeat_cached_call(client, args, "cache-key", prompt_cache_retention="24h", prompt_cache_key=args.cache_key)


def structured(client, args):
    """A response schema pins the shape of the answer. How many tokens it takes to fill
    that shape still differs per model, which is the point of comparing two."""
    for deployment in args.structured_deployments:
        response = client.chat.completions.parse(
            model=deployment,
            messages=INTENT_MESSAGES,
            response_format=IntentEvent,
        )
        print(f"[deployment={deployment}]")
        print(f"  parsed: {response.choices[0].message.parsed}")
        print(f"  completion_tokens: {response.usage.completion_tokens}")
        print(response.usage.model_dump_json(indent=2))


DEMOS = {
    "caching": caching,
    "cache-retention": cache_retention,
    "cache-key": cache_key,
    "structured": structured,
}


def run_demo(client, args, name, position):
    demo = DEMOS[name]
    print(f"\n{BANNER}")
    print(f"{position} {name}")
    print(" ".join(demo.__doc__.split()))  # one line, however the docstring wraps
    print(BANNER)
    demo(client, args)


def main():
    args = parse_args()
    client = create_client(args)
    names = list(DEMOS) if args.demo == "all" else [args.demo]
    for index, name in enumerate(names, start=1):
        run_demo(client, args, name, f"[{index}/{len(names)}]")


if __name__ == "__main__":
    main()
