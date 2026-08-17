#!/usr/bin/env python3
"""Step 0 — the model on its own. No harness at all.

There is no tool, no document and no memory here: one request, one answer, per question.
Everything the model says has to come out of what it already knew, and the report at the
end is the floor every later step is measured against.

Run it before anything else. The point is not that it fails — it is to have the numbers
in front of you when step 1 changes them.
"""

import time

import harness_cli
import harness_loop
import harness_metrics as metrics
from golden import is_hit

# Deliberately the same words the later steps use, minus the sentence about searching.
# If the instructions also changed between steps, a jump in the hit count could be
# credited to the wording, and step 1 would prove nothing about tools.
INSTRUCTIONS = (
    "You answer questions about one Korean housing market report. "
    "Cite every figure you report as [line N], the line it came from. "
    "Say you could not find it rather than guessing. Answer in the language of the question."
)


def parse_args():
    parser = harness_cli.build_parser(
        description="Step 0 — ask the model directly, with no harness, to set a baseline.",
        epilog="Nothing is created in Foundry. The model never sees the document.",
    )
    return harness_cli.finish_parsing(parser)


def ask(ctx, item):
    """One question, one model call. No tools are passed, so the loop never iterates."""
    text, _ = harness_loop.run_turn(ctx, item["question"], None, None, INSTRUCTIONS)
    return text


def main():
    args = parse_args()
    ctx = {**harness_cli.prepare(args), "run": metrics.new_run()}

    metrics.header("step 0 — baseline: no tools, no document, no memory",
                   f"{args.deployment} · {len(ctx['golden'])} questions")

    started = time.perf_counter()
    hits = 0
    for item in ctx["golden"]:
        answer = ask(ctx, item)
        hit = is_hit(item, answer)
        hits += hit
        print(f"  {'hit ' if hit else 'miss'} {item['id']}")
        if args.show_tools:  # nothing to show but the answer itself
            print(f"      {answer.strip()[:200]}")

    metrics.report("baseline (no harness)", ctx["run"], time.perf_counter() - started,
                   hits, len(ctx["golden"]))
    print("\n  Citations here are invented — there is no document to have read them from.")
    print("  Keep this number. Step 1 gives the model a way to look things up.")


if __name__ == "__main__":
    main()
