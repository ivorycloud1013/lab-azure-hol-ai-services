#!/usr/bin/env python3
"""Step 3 — the artifact layer. Somewhere to put work so it is not done twice.

Step 2 kept the conversation small. That is a different problem from this one: a compact
conversation still throws the *work* away. Ask three questions and then ask for a summary
of all three, and an agent with no store goes back and searches the document again for
things it already found ten seconds ago.

The fix is unglamorous — two more tools, save_note and read_note, writing files to a
directory. What changes is that a finding becomes an object with a name, which the agent
can point at later instead of re-deriving.

Run it, then run it with --no-artifacts. The final question is the one to watch.
"""

import os
import re
import tempfile
import time

import harness_cli
import harness_loop
import harness_metrics as metrics
import step1_tools
from golden import is_hit
from harness_metrics import ToolResult

INSTRUCTIONS = (
    "You answer questions about one Korean housing market report. "
    "Always search before answering. "
    "Save each figure you confirm with save_note, and read your earlier notes with "
    "read_note instead of searching for something you already recorded. "
    "Cite every figure you report as [line N], the line it came from. "
    "Answer in the language of the question."
)

INSTRUCTIONS_NO_STORE = step1_tools.INSTRUCTIONS

# Note names come from the model, so they reach the filesystem. Anything outside this
# set of characters is refused — a tool that takes a name and opens a path is the most
# ordinary way an agent harness turns into a file-write primitive.
SAFE_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

MAX_NOTE_CHARS = 4000

# The question that makes the store worth having. It needs every earlier finding at once,
# so an agent without notes has to go back to the document for all of them.
SUMMARY_QUESTION = "앞서 확인한 수치들을 한 표로 정리해 주세요. 각 항목에 근거 줄번호를 남기세요."

STORE_TOOLS = [
    {
        "type": "function",
        "name": "save_note",
        "description": "Save a confirmed finding under a short name so it can be read "
                       "back later without searching the document again.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "short identifier, letters digits dash underscore"},
                "content": {"type": "string",
                            "description": "the finding, including its [line N] citation"},
            },
            "required": ["name", "content"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "read_note",
        "description": "Read back a finding saved earlier with save_note.",
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "the name it was saved under"}},
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "list_notes",
        "description": "List the names of every finding saved so far.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]

TOOLS = step1_tools.TOOLS + STORE_TOOLS


def note_path(directory, name):
    """Refuse anything that is not a plain name before it becomes a path.

    os.path.join with '../../x' from the model would happily escape the directory. The
    check has to happen here, not in the schema — a description is a request, not a
    constraint, and the model is not the only thing that can put a string in this field.
    """
    if not SAFE_NAME.match(name or ""):
        return None
    return os.path.join(directory, f"{name}.md")


def dispatch(ctx, name, arguments):
    """Route the store tools; hand everything else to the dispatcher step 1 built."""
    directory = ctx["artifact_dir"]
    try:
        if name == "save_note":
            path = note_path(directory, arguments["name"])
            if path is None:
                return ToolResult(f"bad note name {arguments['name']!r}, "
                                  "use letters digits dash underscore", False)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(arguments["content"][:MAX_NOTE_CHARS])
            return ToolResult(f"saved {arguments['name']}", True)

        if name == "read_note":
            path = note_path(directory, arguments["name"])
            if path is None or not os.path.isfile(path):
                existing = ", ".join(sorted(list_notes(directory))) or "none"
                return ToolResult(f"no note named {arguments['name']!r}. saved: {existing}", False)
            with open(path, encoding="utf-8") as handle:
                return ToolResult(handle.read(), True)

        if name == "list_notes":
            names = sorted(list_notes(directory))
            return ToolResult(", ".join(names) if names else "no notes saved yet", bool(names))
    except (KeyError, TypeError, ValueError, OSError) as error:
        return ToolResult(f"bad arguments for {name}: {error}", False)

    return step1_tools.dispatch(ctx, name, arguments)


def list_notes(directory):
    return [f[:-3] for f in os.listdir(directory) if f.endswith(".md")]


def reuse_rate(run):
    """Share of tool calls that read a saved finding instead of searching for it again.

    None when the store is off, because zero would read as 'the agent chose not to reuse'
    when in fact it had nothing to reuse.
    """
    calls = run["tool_calls"]
    reads = sum(1 for call in calls if call["name"] in ("read_note", "list_notes"))
    return reads / len(calls) if calls else None


def parse_args():
    parser = harness_cli.build_parser(
        description="Step 3 — build the artifact layer so findings survive the turn that made them.",
        epilog="Run once, then with --no-artifacts. Compare redundant calls on the last question.",
    )
    parser.add_argument("--no-artifacts", dest="artifacts", action="store_false",
                        help="control run: search tools only, nowhere to put anything")
    parser.add_argument("--artifact-dir", default=None, help="where notes are written")
    return harness_cli.finish_parsing(parser)


def main():
    args = parse_args()
    directory = args.artifact_dir or tempfile.mkdtemp(prefix="harness-artifacts-")
    os.makedirs(directory, exist_ok=True)
    ctx = {**harness_cli.prepare(args), "run": metrics.new_run(), "artifact_dir": directory}

    tools = TOOLS if args.artifacts else step1_tools.TOOLS
    router = dispatch if args.artifacts else step1_tools.dispatch
    instructions = INSTRUCTIONS if args.artifacts else INSTRUCTIONS_NO_STORE

    metrics.header("step 3 — artifact store" + ("" if args.artifacts else " (OFF — control run)"),
                   f"{args.deployment} · {len(ctx['golden'])} questions, then one that needs them all")

    started = time.perf_counter()
    previous_id = None
    hits = 0
    for item in ctx["golden"]:
        text, previous_id = harness_loop.run_turn(ctx, item["question"], tools, router,
                                                  instructions, previous_id)
        hit = is_hit(item, text)
        hits += hit
        print(f"  {'hit ' if hit else 'miss'} {item['id']}")

    before = len(ctx["run"]["tool_calls"])
    summary, _ = harness_loop.run_turn(ctx, SUMMARY_QUESTION, tools, router,
                                       instructions, previous_id)
    on_summary = len(ctx["run"]["tool_calls"]) - before
    print(f"  summary question used {on_summary} tool calls")

    rate = reuse_rate(ctx["run"])
    metrics.report("artifact store" + ("" if args.artifacts else " (off)"),
                   ctx["run"], time.perf_counter() - started, hits, len(ctx["golden"]),
                   extra={"artifact reuse": "n/a" if rate is None else f"{rate * 100:.1f}%",
                          "calls on summary": on_summary,
                          "notes written": len(list_notes(directory))})

    print(f"\n  notes: {directory}")
    print("  Redundant calls are the ones that repeated an earlier call exactly. With no")
    print("  store the summary question has to find everything a second time.")
    print("  Next: step 4 asks the agent to decide what it will do before it starts doing it.")


if __name__ == "__main__":
    main()
