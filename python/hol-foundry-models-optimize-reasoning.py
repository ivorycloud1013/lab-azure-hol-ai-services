#!/usr/bin/env python3

import argparse

from openai import OpenAI

import identity

BANNER = "=" * 72

TRIP_QUESTION = "내가 오늘 화성에 한 번 가보고 싶은데, 어떻게 갈 수 있을까 ?"
PUZZLE_QUESTION = "물에 어떤 불순물이 들어갔는데, 100도가 되도 물이 끓지 않아. 어떤 불순물이 들어간 걸까 ?"

PERSON_TOOLS = [
    {
        "type": "function",
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    }
    for name, description in [
        ("get_age", "인물의 나이를 알려주는 함수입니다."),
        ("get_height", "인물의 키를 알려주는 함수입니다."),
        ("get_weight", "인물의 몸무게를 알려주는 함수입니다."),
    ]
]


def parse_args():
    parser = argparse.ArgumentParser(description="Run the gpt-5 optimization knobs from 001-model-002-gpt5-optimization.ipynb.")
    parser.add_argument("--endpoint", required=True, help="Foundry account endpoint")
    parser.add_argument("--deployment", default="gpt-5.4", help="model deployment name")

    identity.add_auth_arguments(parser)

    parser.add_argument(
        "--demo",
        choices=["verbosity", "max-tokens", "cfg", "reasoning", "parallel-tools", "all"],
        default="all",
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


def print_message(response):
    for item in response.output:
        if item.type == "message":
            for content in item.content:
                print(content.text)


def verbosity(client, args):
    """How chatty the answer is. medium is the default."""
    for level in ["low", "medium", "high"]:
        response = client.responses.create(model=args.deployment, input=TRIP_QUESTION, text={"verbosity": level})
        print(f"[verbosity={level:<6}] output_tokens={response.usage.output_tokens}")


def max_tokens(client, args):
    """A cap below what the model wants to write leaves output_text empty, so read the output items."""
    response = client.responses.create(model=args.deployment, input=TRIP_QUESTION, max_output_tokens=1000)
    print("[output]")
    print_message(response)
    print("[usage]")
    print(response.usage.model_dump_json(indent=2))


def cfg(client, args):
    """A custom tool whose grammar is a regex constrains what the model may emit."""
    response = client.responses.create(
        model=args.deployment,
        input="generate_name 을 호출해서 새로 태어나는 아이의 이름을 지어줘.",
        tools=[
            {
                "type": "custom",
                "name": "generate_name",
                "description": "이름을 지어주는 도구입니다.",
                "format": {"type": "grammar", "syntax": "regex", "definition": "^이[가-힣]{1,2}$"},
            }
        ],
    )
    print("[output] tool calls constrained by the regex")
    for item in response.output:
        if item.type == "custom_tool_call":
            print(item.input)
    print("[usage]")
    print(response.usage.model_dump_json(indent=2))


def reasoning(client, args):
    """The same puzzle at the cheap end and the deep end of the reasoning budget."""
    for effort in ["none", "high"]:
        response = client.responses.create(model=args.deployment, input=PUZZLE_QUESTION, reasoning={"effort": effort})
        print(f"[effort={effort}] output_tokens={response.usage.output_tokens}")
        print_message(response)


def parallel_tools(client, args):
    """Three independent lookups the model can request in one turn."""
    response = client.responses.create(
        model=args.deployment,
        input="홍길동의 나이와 키와 몸무게를 알려줘.",
        tools=PERSON_TOOLS,
        parallel_tool_calls=True,
    )
    print("[output] one turn, several tool calls")
    for item in response.output:
        if item.type == "function_call":
            print(f"  {item.name} {item.arguments}")
    print("[usage]")
    print(response.usage.model_dump_json(indent=2))


DEMOS = {
    "verbosity": verbosity,
    "max-tokens": max_tokens,
    "cfg": cfg,
    "reasoning": reasoning,
    "parallel-tools": parallel_tools,
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
