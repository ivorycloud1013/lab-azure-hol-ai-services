#!/usr/bin/env python3

import argparse
import asyncio
import os
import subprocess
from typing import Annotated

from agent_framework import Agent, tool
from agent_framework.foundry import FoundryChatClient
from agent_framework_foundry_hosting import ResponsesHostServer
from pydantic import Field

import identity

INSTRUCTIONS = (
    "You answer questions about one markdown document. "
    "Always search before answering, and quote the line number you relied on. "
    "Say you could not find it rather than guessing. Answer in the language of the question."
)

# Foundry reaches a hosted agent over the responses protocol on this port.
AGENT_PORT = 8088

MAX_MATCHES = 40
CONTEXT_LINES = 2
MAX_CONTEXT_LINES = 10
MAX_PATTERNS = 8
MAX_READ_LINES = 200
GREP_NO_MATCH_RETURNCODE = 1


def resolve_document(path):
    """Find the document whatever the working directory is.

    AGENT_DOCUMENT is relative to the package root, but the container does not
    necessarily start there. prepackage stages the document beside this file, so
    that fallback holds both on Foundry and on this machine.
    """
    if os.path.isfile(path):
        return path
    beside_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
    return beside_script if os.path.isfile(beside_script) else path


def parse_args():
    parser = argparse.ArgumentParser(
        description="A markdown RAG agent. With --question it answers once and exits; without it "
                    "it serves the responses protocol, which is how Foundry hosts it.",
        epilog="The same file runs locally and on Foundry — azd zips this directory and builds it "
               "remotely. See azure.yaml and the Makefile.",
    )
    # azd and Foundry pass these to the agent as environment variables, so they
    # double as the defaults here and stay overridable when running locally.
    parser.add_argument("--endpoint",
                        default=os.getenv("FOUNDRY_PROJECT_ENDPOINT") or os.getenv("AZURE_AI_PROJECT_ENDPOINT"),
                        help="Foundry project endpoint")
    parser.add_argument("--deployment",
                        default=os.getenv("FOUNDRY_MODEL_NAME")
                        or os.getenv("AZURE_AI_MODEL_DEPLOYMENT_NAME", "gpt-5.6-terra"),
                        help="model deployment name")

    identity.add_auth_arguments(parser)

    parser.add_argument("--file", default=os.getenv("AGENT_DOCUMENT"), metavar="MD",
                        help="markdown file to search, or AGENT_DOCUMENT")
    # Foundry never passes this, so the container always takes the serving path.
    parser.add_argument("--question",
                        help="answer this on the terminal and exit, instead of serving")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", AGENT_PORT)))
    parser.add_argument("--host", default="0.0.0.0", help="bind address, 0.0.0.0 when hosted")

    args = parser.parse_args()
    if not args.endpoint:
        parser.error("--endpoint or FOUNDRY_PROJECT_ENDPOINT is required")
    if not args.file:
        parser.error("--file or AGENT_DOCUMENT is required")
    args.file = resolve_document(args.file)
    if not os.path.isfile(args.file):
        parser.error(f"{args.file} not found from {os.getcwd()} — "
                     "run hol-foundry-tools-content-understanding.py first")
    return args


def run_command(command):
    """No shell, so a pattern from the model cannot turn into a second command."""
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode > GREP_NO_MATCH_RETURNCODE:
        return f"command failed: {result.stderr.strip()}"
    return result.stdout


def grep_document(path, patterns, context_lines):
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


def sed_lines(path, start_line, line_count):
    start = max(start_line, 1)
    end = start + min(line_count, MAX_READ_LINES) - 1
    return run_command(["sed", "-n", f"{start},{end}p", path])


def build_tools(path):
    """The document path is closed over, so the tools stay pure functions of their arguments."""

    @tool(approval_mode="never_require")
    def search_document(
        patterns: Annotated[list[str], Field(description="One or more extended regular "
                                                         "expressions, e.g. ['매매가격', '전세|월세']. "
                                                         "A line matching any of them is returned.")],
        context_lines: Annotated[int, Field(description="how many lines to show before and after "
                                                        f"each hit, 0 to {MAX_CONTEXT_LINES}")] = CONTEXT_LINES,
    ) -> str:
        """Search the document for several patterns at once. Returns matching lines
        as line:text, with context_lines lines of surrounding context."""
        return grep_document(path, patterns, context_lines)

    @tool(approval_mode="never_require")
    def read_lines(
        start_line: Annotated[int, Field(description="1-based first line to read")],
        line_count: Annotated[int, Field(description=f"how many lines, at most {MAX_READ_LINES}")],
    ) -> str:
        """Read a line range from the document, for the full context around a hit."""
        return sed_lines(path, start_line, line_count)

    return [search_document, read_lines]


def build_agent(args):
    return Agent(
        name="markdown-rag",
        client=FoundryChatClient(
            project_endpoint=args.endpoint,
            model=args.deployment,
            # Once hosted, this resolves to the agent's managed identity.
            credential=identity.get_credential(args),
        ),
        instructions=f"{INSTRUCTIONS} The document is {os.path.basename(args.file)}.",
        tools=build_tools(args.file),
    )


async def answer_once(agent, question):
    """One turn against the same agent the container serves — no server, no port.

    The context manager closes the chat client, which a long-lived server never
    has to do but a script that exits does.
    """
    async with agent:
        return (await agent.run(question)).text


def main():
    args = parse_args()
    agent = build_agent(args)

    if args.question:
        print(asyncio.run(answer_once(agent, args.question)))
        return

    print(f"serving markdown-rag on {args.host}:{args.port} over {args.file}")
    # Foundry speaks the responses protocol to this process, so it serves
    # POST /responses and nothing else — no browser UI to host.
    ResponsesHostServer(agent).run(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
