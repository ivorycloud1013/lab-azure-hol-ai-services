#!/usr/bin/env python3

import argparse
import sys

from openai import OpenAI

import identity


def parse_args():
    parser = argparse.ArgumentParser(description="Call a model deployed on Foundry with a system/user prompt.")
    parser.add_argument("--endpoint", required=True, help="Foundry account endpoint")
    parser.add_argument("--deployment", default="gpt-5.4", help="model deployment name")

    identity.add_auth_arguments(parser)

    parser.add_argument("--system", default="You are a helpful assistant.")
    parser.add_argument("--user", required=True, help="read from stdin when omitted")
    parser.add_argument("--temperature", type=float)
    parser.add_argument("--max-tokens", type=int)
    parser.add_argument("--stream", action="store_true")

    args = parser.parse_args()
    if args.user is None and not sys.stdin.isatty():
        args.user = sys.stdin.read().strip()
    if not args.user:
        parser.error("--user is required unless a prompt is piped in on stdin")
    return args


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


def build_params(args):
    params = {
        "model": args.deployment,
        "messages": [
            {"role": "system", "content": args.system},
            {"role": "user", "content": args.user},
        ],
    }
    if args.temperature is not None:
        params["temperature"] = args.temperature
    if args.max_tokens is not None:
        # Recent models take max_completion_tokens instead of max_tokens.
        params["max_completion_tokens"] = args.max_tokens
    return params


def main():
    args = parse_args()
    client = create_client(args)
    params = build_params(args)

    if not args.stream:
        response = client.chat.completions.create(**params)
        print(response.choices[0].message.content)
        return

    with client.chat.completions.create(**params, stream=True) as stream:
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                print(chunk.choices[0].delta.content, end="", flush=True)
    print()


if __name__ == "__main__":
    main()
