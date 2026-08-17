#!/usr/bin/env python3
"""step 4 — planning 레이어. 움직이기 전에 수를 정한다.

여기까지는 전부 반응이었다. 모델이 tool 을 부르고, 돌아온 걸 보고, 또 부른다. 발견 두 개를
엮어야 하는 질문이 오기 전까지는 그걸로 된다. 그 뒤로는 반응이 배회로 바뀐다 — 검색하고,
읽고, 옆의 것을 또 검색하고, 다시 읽고, 답은 결국 나오는데 왜 세 번이 아니라 여섯 번
걸렸는지 아무도 말하지 못한다.

planning 은 의도한 경로를 물건으로 만든다. 모델이 먼저 단계를 구조화 출력으로 밝히고,
그다음 실행이 자기가 하겠다던 것과 대조된다. 이 대조가 이 레이어의 값어치 전부다 —
adherence 와 backtracks 를 잴 수 있는 건 오직 계획이 모델 머릿속의 산문이 아니라 데이터로
존재하기 때문이다.

돌려보고 --no-plan 으로 다시 돌리세요. 볼 것은 steps to answer 입니다.
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
    "당신은 한국 주택시장 보고서에 대한 질문에 답하기 전에, 어떻게 답할지 먼저 계획합니다. "
    "부를 tool call 을 순서대로 나열하고 각각 왜 필요한지 밝히세요. "
    "질문이 요구하는 수치를 실제로 찾아낼 수 있는 가장 짧은 경로를 세우세요."
)

# planner 가 이름 붙일 수 있는 tool. step 1 이 등록한 것과 맞춰둔다 — dispatcher 에 없는
# tool 을 부르는 계획은 계획이 아니라 오타이고, 그건 backtrack 으로만 드러난다.
PLANNABLE = ("search_document", "read_lines")

# 이보다 긴 것은 계획이 아니다. 수치 하나에 열 단계를 쓰겠다면 그건 어디를 봐야 할지 모른다는
# 신호이고, 그걸 다 적게 두면 배회를 앞당기면서 값만 두 번 받게 된다.
MAX_PLAN_STEPS = 5


class PlanStep(BaseModel):
    tool: Literal["search_document", "read_lines"]
    why: str = Field(description="이 호출로 무엇을 찾으려 하는지")


class Plan(BaseModel):
    steps: list[PlanStep]


def make_plan(ctx, question):
    """경로를 데이터로 받는다.

    자유 텍스트로도 계획은 나온다. 다만 그걸 뒤에서 확인할 방법이 없다. schema 를 강제하는
    것이 "먼저 검색하겠다고 했다" 를 adherence 로 계산 가능한 값으로 바꾼다.
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
    """의도와 실제를 대조한다.

    adherence 는 계획 중 실제로 수행된 비율, backtracks 는 계획에 없던 호출 수다. 둘은 서로의
    여집합이 아니다 — 계획한 단계를 전부 밟고도 계획에 없는 호출을 네 번 더 할 수 있고,
    바로 그 경우가 볼 만한 경우다.
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
        description="step 4 — planning 레이어를 짓고 계획이 지켜지는지 측정한다.",
        epilog="한 번 돌리고 --no-plan 으로 다시 돌리세요. 계획은 앞에서 호출 하나를 쓰는데, "
               "뒤에서 그만큼을 돌려받는지 보세요.",
    )
    parser.add_argument("--no-plan", dest="plan", action="store_false",
                        help="대조군: step 1 처럼 계획 없이 반응만 한다")
    return harness_cli.finish_parsing(parser)


def main():
    args = parse_args()
    ctx = {**harness_cli.prepare(args), "run": metrics.new_run()}

    metrics.header("step 4 — planning" + ("" if args.plan else " (OFF — 대조군)"),
                   f"{args.deployment} · 질문 {len(ctx['golden'])}개")

    started = time.perf_counter()
    hits = 0
    adherences, backtracks, steps_to_answer = [], 0, []
    for item in ctx["golden"]:
        before = len(ctx["run"]["tool_calls"])

        steps = make_plan(ctx, item["question"]) if args.plan else []
        prompt = plan_as_prompt(item["question"], steps) if steps else item["question"]
        if steps and args.show_tools:
            for step in steps:
                print(f"    계획: {step.tool} — {step.why}")

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
        print(f"  {'hit ' if hit else 'miss'} {item['id']}  호출 {len(calls)}회")

    average = sum(steps_to_answer) / len(steps_to_answer) if steps_to_answer else 0
    extra = {"steps to answer": f"평균 {average:.1f}"}
    if adherences:
        extra["plan adherence"] = f"{sum(adherences) / len(adherences) * 100:.1f}%"
        extra["backtracks"] = backtracks

    metrics.report("planning" + ("" if args.plan else " (꺼짐)"), ctx["run"],
                   time.perf_counter() - started, hits, len(ctx["golden"]), extra=extra)

    print("\n  계획을 세우는 호출도 공짜가 아닙니다 — turns 와 input tokens 에 그대로 잡힙니다.")
    print("  이 레이어가 값어치를 하느냐는, steps to answer 가 그보다 더 줄었느냐입니다.")
    print("  다음: step 5 는 이 모든 것을 확인 가능하게 만들어, 다음 변경을 잴 수 있게 합니다.")


if __name__ == "__main__":
    main()
