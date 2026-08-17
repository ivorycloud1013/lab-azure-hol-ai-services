#!/usr/bin/env python3
"""Step 1 — the tool-calling layer. The first thing that is actually a harness.

Four pieces get built here, and they are four separate decisions:

    1. the schema      what you tell the model the tool is
    2. the dispatcher  what routes a call to real code
    3. the loop        what lets the model come back for more
    4. failure         what a broken call says back

The loop is written out below rather than imported, because writing it once is the
point of this step. harness_loop.py holds the same loop extracted, and steps 2 to 5
import it from there instead of building it again.

Try --broken-tools once. It removes only the fourth piece, and the run dies on the first
malformed call — which is how you find out that error handling is not politeness, it is
what keeps a multi-round agent alive long enough to correct itself.
"""

import json
import time

import harness_cli
import harness_metrics as metrics
import harness_tools
from golden import is_hit
from harness_metrics import ToolResult

INSTRUCTIONS = (
    "You answer questions about one Korean housing market report. "
    "Always search before answering. "
    "Cite every figure you report as [line N], the line it came from. "
    "Say you could not find it rather than guessing. Answer in the language of the question."
)

# Same ceiling harness_loop.py uses. An agent with tools and no round limit is a bill,
# not a feature.
MAX_TOOL_ROUNDS = 8

# Piece 1 — the schema. This is the only description of the tool the model will ever
# see, so everything it needs to use the tool well has to be in here: that patterns is a
# list, that the document is Korean, what the integers count.
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
                                   "e.g. ['전세수급지수', '청약|경쟁률']. "
                                   "A line matching any of them is returned.",
                },
                "context_lines": {
                    "type": "integer",
                    "description": "how many lines to show before and after each hit, "
                                   f"0 to {harness_tools.MAX_CONTEXT_LINES}",
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
                "line_count": {"type": "integer",
                               "description": f"how many lines, at most {harness_tools.MAX_READ_LINES}"},
            },
            "required": ["start_line", "line_count"],
            "additionalProperties": False,
        },
    },
]


# Piece 2 and piece 4 — routing, and what happens when routing fails.
def dispatch(ctx, name, arguments):
    """Route one call, and turn every failure into something the model can act on.

    Three failures are possible and they need three different answers. A miss should
    repeat what was searched for, so the next attempt can use different words. A
    malformed call should name the argument and the problem. An unknown tool should say
    so rather than pretend. Returning the same blank for all three leaves the model with
    no reason to believe a second attempt would go better, and it retries the same
    search under a new spelling until the round budget is gone.

    Letting the exception escape instead — which --broken-tools does — ends the run.
    """
    path = ctx["args"].file
    try:
        if name == "search_document":
            text, matched = harness_tools.grep(
                path, arguments["patterns"], arguments.get("context_lines",
                                                           harness_tools.CONTEXT_LINES))
            return ToolResult(text, matched)
        if name == "read_lines":
            return ToolResult(harness_tools.read(path, arguments["start_line"],
                                                 arguments["line_count"]), True)
        return ToolResult(f"unknown tool {name}", False)
    except (KeyError, TypeError, ValueError) as error:
        # getattr, because steps 2 to 4 import this dispatcher and never define the flag.
        if getattr(ctx["args"], "broken_tools", False):
            raise
        return ToolResult(f"bad arguments for {name}: {error}", False)


# Piece 3 — the loop. Written out here on purpose; steps 2-5 import the extracted copy.
def answer(ctx, question):
    """Ask, run whatever tools come back, ask again, until the model stops asking."""
    args = ctx["args"]
    response = ctx["client"].responses.create(
        model=args.deployment, instructions=INSTRUCTIONS, input=question, tools=TOOLS)
    ctx["run"] = metrics.add_usage(ctx["run"], response.usage)

    for _ in range(MAX_TOOL_ROUNDS):
        outputs = []
        for item in response.output:
            if item.type != "function_call":
                continue
            arguments = json.loads(item.arguments)
            result = dispatch(ctx, item.name, arguments)
            ctx["run"] = metrics.record_tool_call(ctx["run"], item.name, arguments, result)
            if args.show_tools:
                print(f"    {'  ' if result.ok else '! '}{item.name} "
                      f"{json.dumps(arguments, ensure_ascii=False)}")
            # The call_id is what pairs an output with its request. Without it the
            # service cannot tell which answer belongs to which call.
            outputs.append({"type": "function_call_output",
                            "call_id": item.call_id, "output": result.text})

        if not outputs:
            return response.output_text

        response = ctx["client"].responses.create(
            model=args.deployment, previous_response_id=response.id,
            input=outputs, tools=TOOLS)
        ctx["run"] = metrics.add_usage(ctx["run"], response.usage)

    print(f"    [gave up after {MAX_TOOL_ROUNDS} tool rounds]")
    return response.output_text


def parse_args():
    parser = harness_cli.build_parser(
        description="Step 1 — build the tool-calling layer: schema, dispatcher, loop, failures.",
        epilog="Run once normally, then with --broken-tools to see what failure handling buys.",
    )
    parser.add_argument("--broken-tools", action="store_true",
                        help="let tool exceptions escape instead of answering the model")
    return harness_cli.finish_parsing(parser)


def main():
    args = parse_args()
    ctx = {**harness_cli.prepare(args), "run": metrics.new_run()}

    metrics.header("step 1 — tool calling" + (" (failure handling OFF)" if args.broken_tools else ""),
                   f"{args.deployment} · {len(ctx['golden'])} questions · same grep as every other step")

    started = time.perf_counter()
    hits = 0
    for item in ctx["golden"]:
        text = answer(ctx, item["question"])
        hit = is_hit(item, text)
        hits += hit
        print(f"  {'hit ' if hit else 'miss'} {item['id']}")

    metrics.report("tool calling", ctx["run"], time.perf_counter() - started,
                   hits, len(ctx["golden"]))
    print("\n  tool error rate counts calls the dispatcher refused. It is only a number")
    print("  because failure is a return value here — see ToolResult in harness_metrics.py.")
    print("  Next: step 2 asks what happens when one question is not enough.")


if __name__ == "__main__":
    main()
