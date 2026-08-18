#!/usr/bin/env python3
"""step 2 — 검증 레이어. 오답을 하네스가 알아채고, 다시 찾게 한다.

step 1 에서 모델은 답을 찾아왔다. 문제는 그 답이 틀렸을 때도 똑같이 자신 있게 찾아왔다는
것이다. 근거까지 달아서. 하네스는 그걸 그대로 사용자에게 내보냈다.

여기서 짓는 것은 그 사이에 들어가는 한 겹이다.

    답변 → [검증] → 통과면 내보내고, 아니면 사유를 붙여 되돌린다 → 모델이 다시 조사한다

검증이 정답을 알고 있으면 안 된다는 것이 이 단계의 규칙이다. 알고 있으면 하네스가 아니라
채점표이고, 실전에서는 그런 게 없다. 하네스가 확인할 수 있는 것은 하나뿐이다 —
**답이 가리킨 근거가, 그 답을 실제로 뒷받침하는가.** 자세한 것은 harness_verify.py 에 있다.

--no-verify 로 한 번 더 돌리세요. 그게 step 1 입니다. 두 실행의 hit 을 나란히 놓으면
이 한 겹이 무엇을 했는지가 나옵니다.
"""

import time

import golden
import harness_cli
import harness_loop
import harness_metrics as metrics
import harness_verify
import step1_tools

# step 1 과 같은 문장을 쓴다. 지시문까지 손대면 hit 이 오른 것을 문구 덕으로 돌릴 수 있게 되고,
# 그러면 이 단계는 검증에 대해 아무것도 증명하지 못한다.
INSTRUCTIONS = step1_tools.INSTRUCTIONS

# 한 질문에 허용할 시도 횟수. 상한이 없으면 확정되지 않는 질문 하나가 예산을 다 쓴다.
# 3 인 이유는 관측이지 이론이 아니다 — 고쳐질 답은 대개 두 번째에 고쳐진다.
MAX_ATTEMPTS = 3


def ask_once(ctx, prompt, previous_id):
    """한 번 묻고, 이번 시도가 쓴 tool call 을 같이 돌려준다."""
    before = len(ctx["run"]["tool_calls"])
    text, response_id = harness_loop.run_turn(
        ctx, prompt, step1_tools.TOOLS, step1_tools.dispatch, INSTRUCTIONS, previous_id)
    return text, response_id, ctx["run"]["tool_calls"][before:]


def solve(ctx, item, verifying):
    """질문 하나를 통과할 때까지, 또는 시도를 다 쓸 때까지 민다.

    시도마다의 답과 판정을 전부 들고 돌아온다. 마지막 답만 남기면 이 단계가 보여주려는 것 —
    무엇을 집었다가 무엇 때문에 되돌아갔는지 — 이 사라진다.
    """
    attempts = []
    prompt = item["question"]
    previous_id = None

    for number in range(1, (MAX_ATTEMPTS if verifying else 1) + 1):
        text, previous_id, calls = ask_once(ctx, prompt, previous_id)
        verdict = harness_verify.verify(ctx, item, text) if verifying else None
        hit = golden.is_hit(item, text)
        attempts.append({"number": number, "text": text, "calls": calls,
                         "verdict": verdict, "hit": hit})

        metrics.note(f"시도 {number}", f"tool call {len(calls)}번")
        metrics.said(text, limit=160)
        if verdict is None:
            break
        if verdict.ok:
            metrics.note("통과", "인용한 줄이 이 답을 뒷받침합니다")
            break
        metrics.note("반려", f"{verdict.rule} — {verdict.reason}")
        if verdict.next_step:
            metrics.note("다음", verdict.next_step)
        prompt = verdict.feedback()

    return attempts


def tally(results):
    """실행 전체를 네 칸으로 접는다. 이 표가 이 단계의 결론이다.

    검증을 붙였다는 사실만으로는 좋아졌다고 말할 수 없다. 잡아낸 오답과 붙잡은 정답을 함께
    세야 한다. 헛수고 칸이 커지는 검증은 hit 을 올리고도 나쁜 검증이다.
    """
    first_wrong = sum(1 for r in results if not r["attempts"][0]["hit"])
    fixed = sum(1 for r in results
                if not r["attempts"][0]["hit"] and r["attempts"][-1]["hit"])
    stuck = sum(1 for r in results
                if not r["attempts"][0]["hit"] and not r["attempts"][-1]["hit"])
    rejections = [a["verdict"] for r in results for a in r["attempts"]
                  if a["verdict"] is not None and not a["verdict"].ok]
    wasted = sum(1 for r in results for a in r["attempts"]
                 if a["hit"] and a["verdict"] is not None and not a["verdict"].ok)
    cheap = sum(1 for v in rejections if v.rule != "근거 부족")
    stale = sum(1 for v in rejections if v.rule == "폐기된 edition")
    tries = sum(len(r["attempts"]) for r in results)
    return {
        "first_wrong": first_wrong,
        "fixed": fixed,
        "stuck": stuck,
        "rejections": len(rejections),
        "cheap": cheap,
        "stale": stale,
        "wasted": wasted,
        "attempts": tries / len(results) if results else 0,
    }


def parse_args():
    parser = harness_cli.build_parser(
        description="step 2 — 검증 레이어를 짓는다: 오답을 알아채고 다시 찾게 한다.",
        epilog="--no-verify 로 한 번 더 돌리면 그게 step 1 입니다. 두 hit 을 견주세요.",
    )
    parser.add_argument("--no-verify", dest="verify", action="store_false",
                        help="대조군: 검증 없이 첫 답을 그대로 받는다 (= step 1)")
    return harness_cli.finish_parsing(parser)


def main():
    args = parse_args()
    ctx = {**harness_cli.prepare(args), "run": metrics.new_run()}
    total = len(ctx["golden"])

    metrics.header(2, "검증", "오답인 걸 알아채고 다시 찾는다"
                   if args.verify else "검증을 뺀 대조군 — step 1 과 같다")
    metrics.overview([
        ("모델", args.deployment),
        ("검증", f"켜짐 — 결정론 검사 + edition 검사 + 근거 판정, 질문당 최대 {MAX_ATTEMPTS}번 시도"
                 if args.verify else "꺼짐 (대조군)"),
        ("edition", f"있음 — 현행 {golden.CURRENT_EDITION}, "
                    f"대체됨 {', '.join(golden.SUPERSEDED_EDITIONS)}"
                    if args.verify else "없음 (대조군 — step 1 과 같습니다)"),
        ("질문", f"{total}개"),
    ])

    started = time.perf_counter()
    results = []
    for index, item in enumerate(ctx["golden"], start=1):
        metrics.case(index, total, item["question"])
        attempts = solve(ctx, item, args.verify)
        final = attempts[-1]
        first = attempts[0]

        if final["hit"] and len(attempts) > 1:
            metrics.judged(True)
            metrics.note("경과", f"{len(attempts)}번째 시도에서 바로잡았습니다")
        else:
            metrics.judged(final["hit"], golden.missing_keys(item, final["text"]))
        taken = golden.lured(item, first["text"])
        if taken and not first["hit"]:
            metrics.note("함정", f"첫 시도가 집어 온 값: {', '.join(taken)} — {item['lure_why']}")
        results.append({"item": item, "attempts": attempts})

    elapsed = time.perf_counter() - started
    counts = tally(results)
    hits = sum(1 for r in results if r["attempts"][-1]["hit"])

    extra = {"첫 답 오답": f"{counts['first_wrong']}개",
             "평균 시도": f"질문당 {counts['attempts']:.1f}번"}
    if args.verify:
        extra.update({
            "하네스 반려": f"{counts['rejections']}번 (모델 없이 잡은 것 {counts['cheap']}번)",
            "edition 반려": f"{counts['stale']}번 (대체된 문서에만 있는 수치)",
            "재시도로 교정": f"{counts['fixed']}개",
            "재시도 실패": f"{counts['stuck']}개",
            "헛수고": f"{counts['wasted']}번 (맞는 답을 반려)",
        })

    metrics.summary("검증" + ("" if args.verify else " (꺼짐 — 대조군)"),
                    ctx["run"], elapsed, hits, total, extra=extra,
                    command=("python harness/step2_verify.py --endpoint "
                             f"{args.endpoint} --questions {total} --no-verify"))


if __name__ == "__main__":
    main()
