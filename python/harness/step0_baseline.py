#!/usr/bin/env python3
"""step 0 — baseline. 모델 혼자, 하네스가 하나도 없는 상태.

tool 도, 문서도, 기억도 없다. 질문 하나에 요청 하나, 답 하나. 모델이 하는 말은 전부 이미
알고 있던 것에서 나와야 하고, 마지막에 찍히는 리포트가 이후 모든 단계를 재는 바닥이 된다.

무엇보다 먼저 돌리세요. 실패하는 걸 보자는 게 아니라, step 1 이 이 숫자를 바꿀 때 비교할
대상을 손에 쥐고 있자는 겁니다.
"""

import time

import golden
import harness_cli
import harness_loop
import harness_metrics as metrics

# 뒤 단계들이 쓰는 문장에서 "검색하라" 한 줄만 뺀 것이다. instructions 까지 단계마다 달라지면
# hit 이 오른 것을 문구 덕으로 돌릴 수 있게 되고, step 1 은 tool 에 대해 아무것도 증명하지
# 못하게 된다.
INSTRUCTIONS = (
    "당신은 한국 주택시장 보고서에 대해 답합니다. 문서는 여럿일 수 있습니다. "
    "보고하는 모든 수치에 근거를 [문서이름 line N] 형식으로 다세요. "
    "문서이름은 그 수치가 나온 문서의 이름이고, N 은 줄 번호입니다. "
    "찾지 못했으면 추측하지 말고 찾지 못했다고 말하세요. 질문한 언어로 답하세요."
)

def parse_args():
    parser = harness_cli.build_parser(
        description="step 0 — 하네스 없이 모델에 바로 물어 baseline 을 잡는다.",
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
    total = len(ctx["golden"])

    metrics.header(0, "baseline", "하네스 없이 모델만")
    metrics.overview([
        ("모델", args.deployment),
        ("코퍼스", f"문서 {len(ctx['corpus'])}개 — 모델에게는 주지 않습니다"),
        ("질문", f"{total}개"),
    ])

    started = time.perf_counter()
    hits = 0
    invented = 0
    for index, item in enumerate(ctx["golden"], start=1):
        metrics.case(index, total, item["question"])
        answer = ask(ctx, item)

        metrics.said(answer)
        cited = golden.citations(answer)
        invented += len(cited)
        hit = golden.is_hit(item, answer)
        hits += hit

        note = None
        if cited:
            shown = ", ".join(f"{document} line {n}" for document, n in cited[:3])
            note = f"인용한 자리 {shown} — 모델은 이 문서를 본 적이 없습니다. 지어낸 것입니다."
        elif not hit:
            note = "인용이 없습니다. 모르는 것을 모른다고 말한 쪽입니다."
        metrics.judged(hit, golden.missing_keys(item, answer), note)

    elapsed = time.perf_counter() - started
    metrics.summary(
        "baseline", ctx["run"], elapsed, hits, total,
        extra={"지어낸 인용": f"{invented}개"},
        command=(f"python harness/step1_tools.py --endpoint {args.endpoint} "
                 f"--questions {args.questions} --show-tools"))


if __name__ == "__main__":
    main()
