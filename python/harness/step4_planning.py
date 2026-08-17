#!/usr/bin/env python3
"""Step 4 — the planning layer. Decide the moves before making them.

Everything so far reacts: the model calls a tool, sees what came back, calls another.
That works until the question needs two findings combined, and then reacting turns into
wandering — search, read, search something adjacent, read again, and an answer arrives
eventually without anyone able to say why it took six calls instead of three.

Planning makes the intended route an object. The model states the steps first, as
structured output, and then the run can be compared against what it said it would do.
That comparison is the layer's whole value: adherence and backtracks are only
measurable because the plan exists as data rather than as prose in the model's head.

Run it, then run --no-plan. Watch steps to answer.
"""

import time
from typing import Literal

import harness_cli
import harness_loop
import harness_metrics as metrics
import step1_tools
from golden import is_hit
from pydantic import BaseModel, Field

INSTRUCTIONS = step1_tools.INSTRUCTIONS

PLAN_INSTRUCTIONS = (
    "You plan how to answer a question about a Korean housing market report before "
    "answering it. List the tool calls you intend to make, in order, and why each one. "
    "Plan the shortest route that would actually find the figures asked for."
)

# The tools the planner is allowed to name. Kept in sync with what step 1 registered —
# a plan that names a tool the dispatcher does not have is not a plan, it is a typo that
# only shows up as a backtrack.
PLANNABLE = ("search_document", "read_lines")

# A plan longer than this is not a plan. If the model wants ten steps for one figure, the
# useful signal is that it does not know where to look, and letting it write them all out
# just moves the wandering earlier and charges for it twice.
MAX_PLAN_STEPS = 5


class PlanStep(BaseModel):
    tool: Literal["search_document", "read_lines"]
    why: str = Field(description="what this call is meant to find")


class Plan(BaseModel):
    steps: list[PlanStep]


def make_plan(ctx, question):
    """Ask for the route as data.

    Free text would also produce a plan, and nothing downstream could check it. Forcing
    the schema is what turns 'it said it would search first' into a value that adherence
    can be computed from.
    """
    response = ctx["client"].responses.parse(
        model=ctx["args"].deployment,
        instructions=PLAN_INSTRUCTIONS,
        input=f"질문: {question}\n\n어떤 순서로 조사하시겠습니까?",
        text_format=Plan,
    )
    ctx["run"] = metrics.add_usage(ctx["run"], response.usage)
    parsed = response.output_parsed
    return parsed.steps[:MAX_PLAN_STEPS] if parsed else []


def plan_as_prompt(question, steps):
    lines = "\n".join(f"{i}. {s.tool} — {s.why}" for i, s in enumerate(steps, start=1))
    return f"조사 계획:\n{lines}\n\n---\n이 계획대로 진행해서 답하세요.\n질문: {question}"


def score_plan(steps, calls):
    """Compare intent against what happened.

    adherence is how much of the plan was actually carried out, backtracks is how many
    calls were not in it. They are not complements: an agent can follow every planned
    step and still make four unplanned ones, and that is exactly the case worth seeing.
    """
    if not steps:
        return None, len(calls)
    planned = [s.tool for s in steps]
    remaining = list(planned)
    backtracks = 0
    for call in calls:
        if call["name"] in remaining:
            remaining.remove(call["name"])
        else:
            backtracks += 1
    executed = len(planned) - len(remaining)
    return executed / len(planned), backtracks


def parse_args():
    parser = harness_cli.build_parser(
        description="Step 4 — build the planning layer and measure whether the plan is followed.",
        epilog="Run once, then with --no-plan. The plan costs a call up front; see if it "
               "pays for itself in the ones that follow.",
    )
    parser.add_argument("--no-plan", dest="plan", action="store_false",
                        help="control run: answer reactively, the way step 1 did")
    return harness_cli.finish_parsing(parser)


def main():
    args = parse_args()
    ctx = {**harness_cli.prepare(args), "run": metrics.new_run()}

    metrics.header("step 4 — planning" + ("" if args.plan else " (OFF — control run)"),
                   f"{args.deployment} · {len(ctx['golden'])} questions")

    started = time.perf_counter()
    hits = 0
    adherences, backtracks, steps_to_answer = [], 0, []
    for item in ctx["golden"]:
        before = len(ctx["run"]["tool_calls"])

        steps = make_plan(ctx, item["question"]) if args.plan else []
        prompt = plan_as_prompt(item["question"], steps) if steps else item["question"]
        if steps and args.show_tools:
            for step in steps:
                print(f"    plan: {step.tool} — {step.why}")

        text, _ = harness_loop.run_turn(ctx, prompt, step1_tools.TOOLS,
                                        step1_tools.dispatch, INSTRUCTIONS)

        calls = ctx["run"]["tool_calls"][before:]
        adherence, off_plan = score_plan(steps, calls)
        if adherence is not None:
            adherences.append(adherence)
        backtracks += off_plan if steps else 0
        steps_to_answer.append(len(calls))

        hit = is_hit(item, text)
        hits += hit
        print(f"  {'hit ' if hit else 'miss'} {item['id']}  {len(calls)} calls")

    average = sum(steps_to_answer) / len(steps_to_answer) if steps_to_answer else 0
    extra = {"steps to answer": f"{average:.1f} avg"}
    if adherences:
        extra["plan adherence"] = f"{sum(adherences) / len(adherences) * 100:.1f}%"
        extra["backtracks"] = backtracks

    metrics.report("planning" + ("" if args.plan else " (off)"), ctx["run"],
                   time.perf_counter() - started, hits, len(ctx["golden"]), extra=extra)

    print("\n  The planning call is not free — it shows up in turns and input tokens.")
    print("  Whether the layer is worth having is whether steps to answer fell by more.")
    print("  Next: step 5 makes all of this checkable, so the next change can be measured.")


if __name__ == "__main__":
    main()
