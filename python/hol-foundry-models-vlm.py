#!/usr/bin/env python3

import argparse
import base64

from openai import OpenAI

import identity


def parse_args():
    parser = argparse.ArgumentParser(description="Generate or edit an image with a gpt-image model on Foundry.")
    parser.add_argument("--endpoint", required=True, help="Foundry account endpoint")
    parser.add_argument("--deployment", default="gpt-image-2", help="model deployment name")

    identity.add_auth_arguments(parser)

    parser.add_argument("--method", choices=["generate", "edit"], default="generate")
    parser.add_argument("--prompt", help="what to draw, or how to edit --image")
    parser.add_argument("--prompt-file", metavar="TXT", help="read the prompt from this text file instead")
    parser.add_argument("--image", help="source image, required by --method edit")
    parser.add_argument("--mask", help="PNG mask marking the area of --image to replace")
    parser.add_argument("--out", default="image.png", help="output file")
    parser.add_argument("--size", default="1024x1024")
    parser.add_argument("--quality", choices=["low", "medium", "high"], default="low")
    parser.add_argument("--count", type=int, default=1, help="number of images to return")

    args = parser.parse_args()
    if bool(args.prompt) == bool(args.prompt_file):
        parser.error("pass exactly one of --prompt or --prompt-file")
    if args.prompt_file:
        with open(args.prompt_file, encoding="utf-8") as prompt_file:
            args.prompt = prompt_file.read().strip()
    if not args.prompt:
        parser.error(f"{args.prompt_file} is empty")
    if args.method == "edit" and not args.image:
        parser.error("--method edit takes both --image and a prompt")
    if args.method == "generate" and (args.image or args.mask):
        parser.error("--image and --mask only apply to --method edit")
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


def create_image(client, args):
    params = {
        "model": args.deployment,
        "prompt": args.prompt,
        "n": args.count,
        "size": args.size,
        "quality": args.quality,
    }
    if args.method == "generate":
        return client.images.generate(**params)

    # edit takes the source image alongside the prompt.
    params["image"] = open(args.image, "rb")
    if args.mask:
        params["mask"] = open(args.mask, "rb")
    return client.images.edit(**params)


def main():
    args = parse_args()
    result = create_image(create_client(args), args)

    # gpt-image models always return base64, never a URL.
    for index, image in enumerate(result.data):
        path = args.out if index == 0 else f"{index}-{args.out}"
        with open(path, "wb") as out:
            out.write(base64.b64decode(image.b64_json))
        print(path)


if __name__ == "__main__":
    main()
