#!/usr/bin/env python3
"""step 0 — 모델 혼자. 하네스가 하나도 없는 상태.

tool 도, 문서도, 기억도 없다. 질문 하나에 요청 하나, 답 하나. 모델이 하는 말은 전부 이미
알고 있던 것에서 나와야 하고, 마지막에 찍히는 리포트가 이후 모든 단계를 재는 바닥이 된다.

무엇보다 먼저 돌리세요. 실패하는 걸 보자는 게 아니라, step 1 이 이 숫자를 바꿀 때 비교할
대상을 손에 쥐고 있자는 겁니다.
"""

import time

import harness_cli
import harness_loop
import harness_metrics as metrics
from golden import is_hit

# 뒤 단계들이 쓰는 문장에서 "검색하라" 한 줄만 뺀 것이다. instructions 까지 단계마다 달라지면
# hit 이 오른 것을 문구 덕으로 돌릴 수 있게 되고, step 1 은 tool 에 대해 아무것도 증명하지
# 못하게 된다.
INSTRUCTIONS = (
    "당신은 한국 주택시장 보고서 하나에 대해 답합니다. "
    "보고하는 모든 수치에 근거를 [line N] 형식으로 다세요. N 은 그 수치가 나온 줄 번호입니다. "
    "찾지 못했으면 추측하지 말고 찾지 못했다고 말하세요. 질문한 언어로 답하세요."
)


def parse_args():
    parser = harness_cli.build_parser(
        description="step 0 — 하네스 없이 모델에 바로 물어 기준선을 잡는다.",
        epilog="Foundry 에 아무것도 만들지 않습니다. 모델은 문서를 보지 못합니다.",
    )
    return harness_cli.finish_parsing(parser)


def ask(ctx, item):
    """질문 하나에 model 호출 하나. tool 을 안 넘기므로 loop 는 한 번도 돌지 않는다."""
    text, _ = harness_loop.run_turn(ctx, item["question"], None, None, INSTRUCTIONS)
    return text


def main():
    args = parse_args()
    ctx = {**harness_cli.prepare(args), "run": metrics.new_run()}

    metrics.header("step 0 — 기준선: tool 없음, 문서 없음, 기억 없음",
                   f"{args.deployment} · 질문 {len(ctx['golden'])}개")

    started = time.perf_counter()
    hits = 0
    for item in ctx["golden"]:
        answer = ask(ctx, item)
        hit = is_hit(item, answer)
        hits += hit
        print(f"  {'hit ' if hit else 'miss'} {item['id']}")
        if args.show_tools:  # 보여줄 tool 이 없으니 답 자체를 보여준다
            print(f"      {answer.strip()[:200]}")

    metrics.report("기준선 (하네스 없음)", ctx["run"], time.perf_counter() - started,
                   hits, len(ctx["golden"]))
    print("\n  여기서 나온 [line N] 은 지어낸 것입니다 — 읽은 문서가 애초에 없습니다.")
    print("  이 숫자를 기억해 두세요. step 1 에서 모델에게 찾아볼 수단을 줍니다.")


if __name__ == "__main__":
    main()
