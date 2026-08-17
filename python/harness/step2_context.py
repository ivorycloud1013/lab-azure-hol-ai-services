#!/usr/bin/env python3
"""Step 2 — the context-management layer. What the agent carries between turns.

Step 1's loop kept one question alive. A real agent gets asked several things in a row,
and then the question stops being "can it search" and becomes "what is it dragging
along". Three answers, all of them defensible, none of them free:

    full     keep the whole conversation      remembers everything, grows every turn
    summary  compact it every few turns       stays flat, forgets the details
    recall   keep nothing, look it up         stays flat, pays a search per turn

Run --strategy all. The number to watch is context growth: tokens per model call, fitted
across the run. full climbs, the other two do not. Then look at the recall check at the
end, which asks about the very first turn — that is what summary pays for staying flat.
"""

import os
import tempfile
import time

import harness_cli
import harness_loop
import harness_metrics as metrics
import step1_tools
from golden import is_hit

# The same words step 1 used. This step changes what the agent carries between turns,
# nothing about how it is told to behave.
INSTRUCTIONS = step1_tools.INSTRUCTIONS

# How many turns before the summary strategy compacts. Small enough that a short lab run
# actually crosses it — at 4 the fold happens twice in a six-question run, which is what
# makes the flattening visible instead of theoretical.
SUMMARIZE_EVERY = 4

SUMMARY_REQUEST = (
    "지금까지의 대화에서 확인된 사실만 목록으로 정리하세요. "
    "각 항목에 근거 줄번호를 [line N] 형식으로 남기세요. 추측은 넣지 마세요."
)

# Lines of transcript the recall strategy hands back per turn. Large enough to carry an
# earlier finding, small enough that it cannot quietly become the full strategy again.
RECALL_LINES = 12


def transcript_path(directory):
    return os.path.join(directory, "transcript.md")


def append_transcript(path, question, answer):
    """The recall strategy's memory. A file, on purpose — it is the same thing step 3
    turns into a first-class artifact store, and seeing it start as one flat log makes
    that step's argument easier to follow."""
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"\n## {question}\n{answer.strip()}\n")


def recall_context(path):
    """Pull the tail of the transcript back into the next prompt.

    Deliberately dumb — the last N lines, no ranking. A retrieval step good enough to
    argue about would make this step about retrieval quality instead of about the choice
    between carrying history and fetching it.
    """
    if not os.path.isfile(path):
        return ""
    with open(path, encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    return "\n".join(lines[-RECALL_LINES:])


def run_strategy(ctx, name, directory):
    """Ask every question in one conversation, under one context strategy."""
    args = ctx["args"]
    path = transcript_path(os.path.join(directory, name))
    os.makedirs(os.path.dirname(path), exist_ok=True)

    previous_id = None
    summary = ""
    hits = 0
    for index, item in enumerate(ctx["golden"], start=1):
        prompt = item["question"]
        if name == "recall":
            memory = recall_context(path)
            if memory:
                prompt = f"이전에 확인한 내용:\n{memory}\n\n---\n{item['question']}"
        elif name == "summary" and summary:
            prompt = f"지금까지 확인한 내용:\n{summary}\n\n---\n{item['question']}"

        text, response_id = harness_loop.run_turn(
            ctx, prompt, step1_tools.TOOLS, step1_tools.dispatch,
            INSTRUCTIONS, previous_id)

        hit = is_hit(item, text)
        hits += hit
        print(f"  {'hit ' if hit else 'miss'} turn {index}  {item['id']}")

        if name == "full":
            previous_id = response_id
        elif name == "summary":
            previous_id = response_id
            if index % SUMMARIZE_EVERY == 0:
                # Fold the chain into text, then drop the chain. Keeping both would keep
                # the growth the summary exists to stop.
                summary, _ = harness_loop.run_turn(
                    ctx, SUMMARY_REQUEST, None, None, INSTRUCTIONS, previous_id)
                previous_id = None
                print(f"       [compacted after turn {index}]")
        else:
            append_transcript(path, item["question"], text)

    return hits


def check_recall(ctx, first_item, previous_id=None):
    """Ask about the first turn, last. This is the bill for staying flat.

    A strategy that never grew has to have kept the early finding somewhere, or it
    cannot answer this — and a flat token curve with a failed recall is not a win.
    """
    question = f"앞서 확인한 내용 중, {first_item['question']} 다시 알려주세요."
    text, _ = harness_loop.run_turn(ctx, question, step1_tools.TOOLS,
                                    step1_tools.dispatch, INSTRUCTIONS, previous_id)
    return is_hit(first_item, text)


def parse_args():
    parser = harness_cli.build_parser(
        description="Step 2 — build the context-management layer and compare three strategies.",
        epilog="Watch context growth. full climbs, summary and recall stay flat, and the "
               "recall check at the end says what that flatness cost.",
    )
    parser.add_argument("--strategy", choices=["full", "summary", "recall", "all"],
                        default="all")
    parser.add_argument("--out-dir", default=None,
                        help="where the recall strategy keeps its transcript")
    return harness_cli.finish_parsing(parser)


def main():
    args = parse_args()
    base = harness_cli.prepare(args)
    directory = args.out_dir or tempfile.mkdtemp(prefix="harness-context-")

    names = ["full", "summary", "recall"] if args.strategy == "all" else [args.strategy]
    for name in names:
        ctx = {**base, "run": metrics.new_run()}
        metrics.header(f"step 2 — context management: {name}",
                       f"{args.deployment} · {len(ctx['golden'])} turns in one conversation")
        started = time.perf_counter()
        hits = run_strategy(ctx, name, directory)
        recalled = check_recall(ctx, ctx["golden"][0])
        metrics.report(f"context management ({name})", ctx["run"],
                       time.perf_counter() - started, hits, len(ctx["golden"]),
                       extra={"recall check": "kept" if recalled else "LOST"})

    print(f"\n  transcripts: {directory}")
    print("  Context growth is tokens per model call, fitted over the run. A flat curve")
    print("  with a LOST recall is not a saving — it is the agent forgetting on schedule.")
    print("  Next: step 3 gives it somewhere to put things it should not have to re-derive.")


if __name__ == "__main__":
    main()
