#!/usr/bin/env python3

import argparse
import json
import os
import subprocess

from openai import OpenAI

import identity

INSTRUCTIONS = (
    "You answer questions about one markdown document. "
    "Always search before answering, and quote the line number you relied on. "
    "Say you could not find it rather than guessing. Answer in the language of the question."
)

MAX_TOOL_ROUNDS = 8
MAX_MATCHES = 40
CONTEXT_LINES = 2
MAX_CONTEXT_LINES = 10
MAX_PATTERNS = 8
MAX_READ_LINES = 200
GREP_NO_MATCH_RETURNCODE = 1

# Retrieval is grep over the markdown Content Understanding produced — no vector
# store, so no embedding deployment and no index to keep in sync. The model
# picks the search terms and every hit is quotable back to a line number.
TOOLS = [
    {
        "type": "function",
        "name": "search_document",
        "description": "Search the document for several regular expressions at once. "
                       "Returns matching lines as line:text with surrounding context. "
                       "Search in the language the document is written in.",
        "parameters": {
            "type": "object",
            "properties": {
                "patterns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "One or more extended regular expressions, "
                                   "e.g. ['헌법', '기본권|자유']. A line matching any of them is returned.",
                },
                "context_lines": {
                    "type": "integer",
                    "description": f"how many lines to show before and after each hit, 0 to {MAX_CONTEXT_LINES}",
                },
            },
            "required": ["patterns", "context_lines"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "read_lines",
        "description": "Read a line range from the document, "
                       "to see the full context around a search hit.",
        "parameters": {
            "type": "object",
            "properties": {
                "start_line": {"type": "integer", "description": "1-based first line to read"},
                "line_count": {"type": "integer", "description": f"how many lines, at most {MAX_READ_LINES}"},
            },
            "required": ["start_line", "line_count"],
            "additionalProperties": False,
        },
    },
]


def parse_args():
    parser = argparse.ArgumentParser(
        description="Answer questions over a local markdown file with an ephemeral agent on the Responses API.",
        epilog="The agent lives only in this process — nothing is created in Foundry.",
    )
    parser.add_argument("--endpoint", required=True, help="Foundry account or project endpoint")
    parser.add_argument("--deployment", default="gpt-5.6-terra", help="model deployment name")

    identity.add_auth_arguments(parser)

    parser.add_argument("--file", required=True, metavar="MD", help="markdown file to search")
    parser.add_argument("--question", required=True, help="what to ask about the document")
    parser.add_argument("--show-tools", action="store_true", help="print each search the agent runs")

    args = parser.parse_args()
    if not os.path.isfile(args.file):
        parser.error(f"{args.file} not found — run hol-foundry-tools-content-understanding.py first")
    return args


def run_command(command):
    """No shell, so a pattern from the model cannot turn into a second command."""
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode > GREP_NO_MATCH_RETURNCODE:
        return f"command failed: {result.stderr.strip()}"
    return result.stdout


def search_document(path, patterns, context_lines=CONTEXT_LINES):
    """Match any of the patterns in one pass. One call with several patterns beats
    several calls, because the hits come back interleaved in document order."""
    kept = [p for p in patterns if p][:MAX_PATTERNS]
    if not kept:
        return "no pattern given"
    command = ["grep", "--line-number", "--ignore-case", "--extended-regexp",
               f"--context={max(0, min(context_lines, MAX_CONTEXT_LINES))}"]
    for pattern in kept:
        command += ["-e", pattern]

    output = run_command(command + ["--", path])
    lines = output.splitlines()
    if not lines:
        return f"no match for {' | '.join(kept)}"
    if len(lines) > MAX_MATCHES:
        return "\n".join(lines[:MAX_MATCHES]) + (
            f"\n… {len(lines) - MAX_MATCHES} more lines, narrow the patterns "
            "or lower context_lines")
    return output


def read_lines(path, start_line, line_count):
    start = max(start_line, 1)
    end = start + min(line_count, MAX_READ_LINES) - 1
    return run_command(["sed", "-n", f"{start},{end}p", path])


def call_tool(path, name, arguments):
    """Dispatch one tool call. Errors come back as text so the model can correct
    itself instead of the run dying."""
    try:
        if name == "search_document":
            return search_document(path, arguments["patterns"],
                                   arguments.get("context_lines", CONTEXT_LINES))
        if name == "read_lines":
            return read_lines(path, arguments["start_line"], arguments["line_count"])
        return f"unknown tool {name}"
    except (KeyError, TypeError, ValueError) as error:
        return f"bad arguments for {name}: {error}"


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


def run_tool_calls(args, response):
    """Answer every function call in this response. Returns [] when the agent is done."""
    outputs = []
    for item in response.output:
        if item.type != "function_call":
            continue
        arguments = json.loads(item.arguments)
        if args.show_tools:
            print(f"[{item.name} {json.dumps(arguments, ensure_ascii=False)}]")
        outputs.append({
            "type": "function_call_output",
            "call_id": item.call_id,
            "output": call_tool(args.file, item.name, arguments),
        })
    return outputs


def answer(client, args):
    response = client.responses.create(
        model=args.deployment,
        instructions=f"{INSTRUCTIONS} The document is {os.path.basename(args.file)}.",
        input=args.question,
        tools=TOOLS,
    )
    for _ in range(MAX_TOOL_ROUNDS):
        outputs = run_tool_calls(args, response)
        if not outputs:
            return response.output_text
        # previous_response_id carries the history, so only the new outputs go up.
        response = client.responses.create(
            model=args.deployment,
            previous_response_id=response.id,
            input=outputs,
            tools=TOOLS,
        )
    raise SystemExit(f"the agent kept searching past {MAX_TOOL_ROUNDS} rounds, narrow the question")


def main():
    args = parse_args()
    print(answer(create_client(args), args))


if __name__ == "__main__":
    main()
