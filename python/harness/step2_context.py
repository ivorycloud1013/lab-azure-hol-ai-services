#!/usr/bin/env python3
"""step 2 — context 관리 레이어. agent 가 turn 사이에 무엇을 들고 가는가.

step 1 의 loop 는 질문 하나를 살려냈다. 실제 agent 는 여러 개를 연달아 받고, 그때부터 질문은
"검색할 수 있는가" 가 아니라 "무엇을 끌고 다니는가" 가 된다. 답은 셋이고, 셋 다 말이 되며,
셋 다 공짜가 아니다.

    full     대화를 통째로 유지        전부 기억하고, turn 마다 커진다
    summary  몇 turn 마다 압축          평평하게 유지되고, 세부를 잊는다
    recall   아무것도 안 들고 필요하면 찾음  평평하게 유지되고, turn 마다 검색 비용을 낸다

--strategy all 로 돌리세요. 볼 숫자는 context growth — model 호출당 token 을 실행 전체에
회귀시킨 기울기입니다. full 은 올라가고 나머지 둘은 안 올라갑니다. 그다음 맨 끝의 recall
check 를 보세요. 첫 turn 에 대해 묻는데, 그게 summary 가 평평함의 대가로 치른 것입니다.
"""

import os
import tempfile
import time

import harness_cli
import harness_loop
import harness_metrics as metrics
import step1_tools
from golden import is_hit

# step 1 과 같은 문장. 이 단계가 바꾸는 것은 agent 가 turn 사이에 들고 가는 것이지,
# 어떻게 행동하라고 말해주는 내용이 아니다.
INSTRUCTIONS = step1_tools.INSTRUCTIONS

# summary 전략이 몇 turn 마다 압축하는가. 짧은 랩 실행에서도 실제로 넘어가도록 작게 잡았다 —
# 4면 질문 6개짜리 실행에서 두 번 접히고, 그래야 평평해지는 게 이론이 아니라 눈에 보인다.
SUMMARIZE_EVERY = 4

SUMMARY_REQUEST = (
    "지금까지의 대화에서 확인된 사실만 목록으로 정리하세요. "
    "각 항목에 근거 줄번호를 [line N] 형식으로 남기세요. 추측은 넣지 마세요."
)

# recall 전략이 turn 마다 되돌려주는 transcript 줄 수. 앞선 발견을 실어 나를 만큼은 크고,
# 슬그머니 full 전략이 되어버릴 만큼 크지는 않게.
RECALL_LINES = 12


def transcript_path(directory):
    return os.path.join(directory, "transcript.md")


def append_transcript(path, question, answer):
    """recall 전략의 기억. 일부러 파일이다 — step 3 이 이걸 제대로 된 artifact store 로
    바꾸는데, 여기서 평평한 로그 하나로 시작하는 걸 봐두면 그 단계의 주장이 따라가기 쉽다."""
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"\n## {question}\n{answer.strip()}\n")


def recall_context(path):
    """transcript 의 꼬리를 다음 prompt 로 되가져온다.

    일부러 멍청하게 만들었다 — 마지막 N 줄, 순위 매기기 없음. 논쟁할 만큼 좋은 검색을 넣으면
    이 단계가 "히스토리를 들고 갈까 가져올까" 가 아니라 검색 품질에 대한 이야기가 된다.
    """
    if not os.path.isfile(path):
        return ""
    with open(path, encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    return "\n".join(lines[-RECALL_LINES:])


def run_strategy(ctx, name, directory):
    """한 대화 안에서 모든 질문을, 하나의 context 전략으로 묻는다."""
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
                # 대화 사슬을 글로 접은 다음, 사슬을 끊는다. 둘 다 들고 있으면 summary 가
                # 막으려던 그 증가를 그대로 안고 가게 된다.
                summary, _ = harness_loop.run_turn(
                    ctx, SUMMARY_REQUEST, None, None, INSTRUCTIONS, previous_id)
                previous_id = None
                print(f"       [turn {index} 뒤 압축]")
        else:
            append_transcript(path, item["question"], text)

    return hits


def check_recall(ctx, first_item, previous_id=None):
    """맨 마지막에 첫 turn 을 묻는다. 평평함의 청구서다.

    한 번도 안 커진 전략은 초기 발견을 어딘가에 남겨뒀어야 이 질문에 답할 수 있다.
    token 곡선이 평평한데 recall 이 실패했다면 그건 이긴 게 아니다.
    """
    question = f"앞서 확인한 내용 중, {first_item['question']} 다시 알려주세요."
    text, _ = harness_loop.run_turn(ctx, question, step1_tools.TOOLS,
                                    step1_tools.dispatch, INSTRUCTIONS, previous_id)
    return is_hit(first_item, text)


def parse_args():
    parser = harness_cli.build_parser(
        description="step 2 — context 관리 레이어를 짓고 세 전략을 비교한다.",
        epilog="context growth 를 보세요. full 은 올라가고 summary 와 recall 은 평평합니다. "
               "맨 끝 recall check 가 그 평평함의 대가를 말해줍니다.",
    )
    parser.add_argument("--strategy", choices=["full", "summary", "recall", "all"],
                        default="all")
    parser.add_argument("--out-dir", default=None,
                        help="recall 전략이 transcript 를 남길 곳")
    return harness_cli.finish_parsing(parser)


def main():
    args = parse_args()
    base = harness_cli.prepare(args)
    directory = args.out_dir or tempfile.mkdtemp(prefix="harness-context-")

    names = ["full", "summary", "recall"] if args.strategy == "all" else [args.strategy]
    for name in names:
        ctx = {**base, "run": metrics.new_run()}
        metrics.header(f"step 2 — context 관리: {name}",
                       f"{args.deployment} · 한 대화 안에서 {len(ctx['golden'])} turn")
        started = time.perf_counter()
        hits = run_strategy(ctx, name, directory)
        recalled = check_recall(ctx, ctx["golden"][0])
        metrics.report(f"context 관리 ({name})", ctx["run"],
                       time.perf_counter() - started, hits, len(ctx["golden"]),
                       extra={"recall check": "기억함" if recalled else "잊음"})

    print(f"\n  transcript: {directory}")
    print("  context growth 는 model 호출당 token 을 실행 전체에 회귀시킨 기울기입니다.")
    print("  곡선이 평평한데 recall 이 '잊음' 이면 그건 절약이 아니라 예정대로 잊은 것입니다.")
    print("  다음: step 3 은 다시 구할 필요 없는 것을 둘 자리를 만들어 줍니다.")


if __name__ == "__main__":
    main()
