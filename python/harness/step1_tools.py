#!/usr/bin/env python3
"""step 1 — tool call 레이어. 처음으로 하네스라 부를 만한 것.

여기서 네 조각을 짓는데, 넷은 서로 다른 결정이다.

    1. schema      모델에게 이 tool 이 무엇이라고 말해줄 것인가
    2. dispatcher  호출을 실제 코드로 보내는 것
    3. loop        모델이 더 부르러 돌아올 수 있게 하는 것
    4. 실패 처리    잘못된 호출에 뭐라고 답해줄 것인가

loop 를 import 하지 않고 아래에 직접 적은 이유는, 한 번 써보는 것이 이 단계의 목적이라서다.
harness_loop.py 에 같은 loop 를 뽑아 두었고 step 2 부터 5 는 거기서 가져다 쓴다.

--broken-tools 를 한 번 돌려보세요. 네 번째 조각만 빼는데, 첫 번째 잘못된 호출에서 실행이
죽습니다. 실패 처리가 예의가 아니라 여러 round 를 도는 agent 가 스스로 고칠 만큼 살아 있게
해주는 장치라는 걸 그때 알게 됩니다.
"""

import json
import time

import golden
import harness_cli
import harness_metrics as metrics
import harness_tools
from harness_metrics import ToolResult

DOES = ("step 0 과 똑같은 질문을 똑같은 모델에게 묻습니다. 달라진 것은 하나뿐입니다 — "
        "모델이 문서를 검색하고 읽을 수 있는 tool 두 개를 줍니다.")

WATCH = ("맞힌 개수보다, 틀린 답이 어떻게 생겼는지를 보세요. 모델은 이제 문서를 봅니다. "
         "그래서 step 0 처럼 허공에서 지어내지 않습니다 — 대신 이미 대체된 문서에서 집어 옵니다. "
         "코퍼스에는 같은 보고서가 두 판 들어 있고, 검색은 둘 다 물어옵니다. "
         "어느 쪽이 살아 있는 판인지는 문서 어디에도 적혀 있지 않고, 이 단계의 하네스는 "
         "그걸 모릅니다. 그래서 모델은 둘 중 하나를 근거까지 달아 자신 있게 내놓습니다. "
         "맨 아래 '자신 있게 틀림' 이 그 개수입니다.")

INSTRUCTIONS = (
    "당신은 한국 주택시장 보고서에 대해 답합니다. 문서는 여럿일 수 있습니다. "
    "답하기 전에 반드시 먼저 검색하세요. "
    "보고하는 모든 수치에 근거를 [문서이름 line N] 형식으로 다세요. "
    "문서이름은 검색 결과에 붙어 오는 이름이고, N 은 그 수치가 나온 줄 번호입니다. "
    "찾지 못했으면 추측하지 말고 찾지 못했다고 말하세요. 질문한 언어로 답하세요."
)

# harness_loop.py 와 같은 상한. tool 이 있고 round 제한이 없는 agent 는 기능이 아니라 청구서다.
MAX_TOOL_ROUNDS = 8

# 조각 1 — schema. 모델이 이 tool 에 대해 보게 될 유일한 설명이라, tool 을 잘 쓰는 데 필요한
# 것이 전부 여기 들어 있어야 한다. patterns 가 배열이라는 것, 문서가 한국어라는 것,
# 정수 인자가 무엇을 세는지까지.
TOOLS = [
    {
        "type": "function",
        "name": "search_document",
        "description": "코퍼스의 모든 문서에서 여러 정규식을 한 번에 검색한다. "
                       "일치한 줄을 문서이름:line:text 형식으로, 주변 맥락과 함께 돌려준다. "
                       "문서가 쓰인 언어로 검색할 것.",
        "parameters": {
            "type": "object",
            "properties": {
                "patterns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "확장 정규식 하나 이상. 예: ['전세수급지수', '청약|경쟁률'] "
                                   "이 중 하나라도 일치하는 줄이 돌아온다.",
                },
                "context_lines": {
                    "type": "integer",
                    "description": "각 일치 줄의 앞뒤로 몇 줄을 함께 보여줄지, "
                                   f"0 에서 {harness_tools.MAX_CONTEXT_LINES} 사이",
                },
            },
            "required": ["patterns", "context_lines"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "read_lines",
        "description": "한 문서의 줄 범위를 읽는다. 검색으로 찾은 자리의 전체 맥락을 볼 때 쓴다.",
        "parameters": {
            "type": "object",
            "properties": {
                "document": {"type": "string",
                             "description": "읽을 문서 이름. 검색 결과 줄 앞에 붙어 온 그 이름."},
                "start_line": {"type": "integer", "description": "읽기 시작할 줄, 1부터 셈"},
                "line_count": {"type": "integer",
                               "description": f"몇 줄을 읽을지, 최대 {harness_tools.MAX_READ_LINES}"},
            },
            "required": ["document", "start_line", "line_count"],
            "additionalProperties": False,
        },
    },
]


# 조각 2 와 조각 4 — 어디로 보낼 것인가, 그리고 그게 실패하면 어떻게 할 것인가.
def dispatch(ctx, name, arguments):
    """호출 하나를 보내고, 모든 실패를 모델이 손댈 수 있는 것으로 바꾼다.

    실패는 세 가지이고 세 가지 다른 답이 필요하다. 못 찾았으면 무엇을 찾았는지 되풀이해 줘야
    다음 시도에서 다른 말로 바꿔볼 수 있다. 인자가 틀렸으면 어느 인자가 왜 틀렸는지 말해야
    한다. 없는 tool 이면 없다고 해야지 있는 척하면 안 된다. 셋 다 똑같은 빈 답을 주면 모델은
    두 번째 시도가 더 나을 거라 믿을 근거가 없고, 결국 같은 검색을 철자만 바꿔가며 round
    예산이 떨어질 때까지 반복한다.

    대신 예외를 그대로 올려보내면 — --broken-tools 가 하는 일이 그것이다 — 실행이 끝난다.
    """
    corpus = ctx["corpus"]
    try:
        if name == "search_document":
            text, matched = harness_tools.grep(
                [entry["path"] for entry in corpus.values()],
                arguments["patterns"],
                arguments.get("context_lines", harness_tools.CONTEXT_LINES))
            return ToolResult(text, matched)
        if name == "read_lines":
            # 없는 문서 이름은 네 번째 조각이 다루는 실패의 하나다. 예외로 죽이지 않고,
            # 무엇이 있는지 되돌려줘야 모델이 다음 시도에서 고칠 수 있다.
            document = arguments["document"]
            if document not in corpus:
                return ToolResult(
                    f"{document} 는 없는 문서입니다. 있는 문서: {', '.join(corpus)}", False)
            return ToolResult(harness_tools.read(corpus[document]["path"],
                                                 arguments["start_line"],
                                                 arguments["line_count"]), True)
        return ToolResult(f"{name} 은 없는 tool 입니다", False)
    except (KeyError, TypeError, ValueError) as error:
        # getattr 인 이유 — step 2 부터 4 가 이 dispatcher 를 가져다 쓰는데 그 플래그가 없다.
        if getattr(ctx["args"], "broken_tools", False):
            raise
        return ToolResult(f"{name} 의 인자가 잘못되었습니다: {error}", False)


# 조각 3 — loop. 일부러 여기 펼쳐 썼다. step 2~5 는 뽑아둔 사본을 가져다 쓴다.
def answer(ctx, question):
    """묻고, 돌아온 tool 을 실행하고, 다시 묻는다. 모델이 그만 부를 때까지."""
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
            # call_id 가 결과와 요청을 짝지어 준다. 이게 없으면 서비스는 어느 답이 어느
            # 호출의 것인지 알 수 없다.
            outputs.append({"type": "function_call_output",
                            "call_id": item.call_id, "output": result.text})

        if not outputs:
            return response.output_text

        # instructions 를 매 round 다시 붙인다. previous_response_id 는 *대화* 를 이어주지
        # *지시문* 을 이어주지 않는다 — 서비스가 그렇게 설계되어 있다. 이걸 빼면 첫 호출만
        # 지시문을 보고, 정작 모델이 답을 쓰는 마지막 호출은 아무 지시 없이 돈다. 그러면
        # "[line N] 근거를 다세요" 가 사라져서 인용 없는 답이 돌아오고, step 2 의 검증은
        # 오답이 아니라 그 빠진 인용을 잡느라 헛수고만 쌓는다.
        response = ctx["client"].responses.create(
            model=args.deployment, instructions=INSTRUCTIONS,
            previous_response_id=response.id, input=outputs, tools=TOOLS)
        ctx["run"] = metrics.add_usage(ctx["run"], response.usage)

    print(f"    [tool round {MAX_TOOL_ROUNDS}회를 넘겨 중단]")
    return response.output_text


def summarize(total, hits, confident, hedged):
    """이번 실행에서 실제로 일어난 일만 말한다.

    문구를 실패가 있다고 가정하고 써두면, 다 맞힌 실행에서 "모델은 확신했고 하네스는 그대로
    내보냈습니다" 같은 문장이 0개 옆에 붙는다. 학습자가 이 랩에서 배우는 것은 숫자 읽는
    법인데, 숫자와 문장이 어긋나면 그때부터 문장을 안 읽거나 숫자를 안 믿는다.
    """
    lead = f"질문 {total}개 중 {hits}개를 맞혔습니다. "
    tail = " grep 자체는 step 0 이후로 한 글자도 바뀌지 않았습니다."
    parts = []
    if confident:
        parts.append(f"{confident}개는 대체된 문서를 근거로 달고 틀렸습니다 — "
                     "모델은 확신했고, 하네스는 그대로 내보냈습니다")
    if hedged:
        parts.append(f"{hedged}개는 두 판의 값을 나란히 적고 고르지 않았습니다 — "
                     "충돌은 알아봤지만 어느 쪽이 유효한지는 어느 문서에도 없습니다")
    if parts:
        body = f"틀린 {total - hits}개 중 " + ", ".join(parts) + "."
    elif hits < total:
        body = (f"틀린 {total - hits}개는 근거를 달지 않았습니다. "
                "그래도 하네스가 걸러낸 것은 없습니다 — 근거가 있든 없든 그대로 나갔습니다.")
    else:
        # 다 맞힌 실행이 하네스가 일했다는 뜻은 아니다. 이 단계에는 아직 확인하는 겹이 없다.
        body = ("틀린 답이 없었습니다. 하지만 그건 이 하네스가 한 일이 아닙니다 — "
                "답이 맞는지 아무도 보지 않았고, 틀렸어도 똑같이 나갔을 것입니다.")
    return lead + body + tail


def parse_args():
    parser = harness_cli.build_parser(
        description="step 1 — tool call 레이어를 짓는다: schema, dispatcher, loop, 실패 처리.",
        epilog="먼저 그냥 돌리고, 그다음 --broken-tools 로 돌려 실패 처리가 무엇을 사는지 보세요.",
    )
    parser.add_argument("--broken-tools", action="store_true",
                        help="tool 예외를 모델에게 답하지 않고 그대로 올려보낸다")
    return harness_cli.finish_parsing(parser)


def main():
    args = parse_args()
    ctx = {**harness_cli.prepare(args), "run": metrics.new_run()}
    total = len(ctx["golden"])
    broken = args.broken_tools

    metrics.header(1, "tool call", "모델에게 문서를 찾을 손을 준다"
                   + (" — 실패 처리는 뺀 채로" if broken else ""))
    metrics.overview(DOES, WATCH, [
        ("모델", args.deployment),
        ("코퍼스", f"문서 {len(ctx['corpus'])}개: {', '.join(ctx['corpus'])} — 이제 검색할 수 있습니다"),
        ("판 정보", "없음 — 이 단계의 하네스는 어느 문서가 유효한지 모릅니다"),
        ("tool", "search_document, read_lines — grep 과 sed, 모든 단계에서 동일"),
        ("실패 처리", "꺼짐 (예외가 그대로 올라옴)" if broken else "켜짐 (문장으로 되돌려줌)"),
    ])

    started = time.perf_counter()
    hits = 0
    confident = 0   # 근거를 달고 낸 오답. step 2 가 잡으러 가는 것이 이 숫자다
    hedged = 0      # 충돌은 알아챘는데 고르지 못하고 양쪽을 다 적은 답
    for index, item in enumerate(ctx["golden"], start=1):
        metrics.case(index, total, item["question"])
        before = len(ctx["run"]["tool_calls"])
        text = answer(ctx, item["question"])
        calls = ctx["run"]["tool_calls"][before:]

        metrics.used(calls)
        metrics.said(text)
        hit = golden.is_hit(item, text)
        hits += hit

        note = None
        empty = sum(1 for call in calls if not call["ok"])
        taken = golden.lured(item, text)
        cited = golden.citations(text)
        # 양쪽 값을 다 적었나. 충돌을 알아본 것이지만 답은 아니다 — 고르는 일을 사용자에게
        # 떠넘긴 것이고, 사용자에게는 고를 근거가 더 없다.
        both = bool(taken) and not golden.missing_keys(item, text)
        if both:
            note = (f"두 판의 값을 나란히 적고 고르지 않았습니다: {', '.join(taken)} 와 "
                    f"{', '.join(item['answer_key'])}. 충돌은 알아봤지만, 어느 쪽이 유효한지는 "
                    "어느 문서에도 없어서 모델이 알 수가 없습니다.")
        elif not hit and taken:
            # 이 랩이 다루는 실패다. 못 찾은 게 아니라 대체된 문서에서 찾아왔고, 근거까지 달았다.
            note = (f"대체된 문서의 값을 집어 왔습니다: {', '.join(taken)} — {item['lure_why']}. "
                    f"근거는 {len(cited)}개 달려 있습니다. 하네스는 이게 오답인 줄 모릅니다.")
        elif not hit and empty:
            note = (f"검색 {empty}번이 빈손이었습니다. dispatcher 가 무엇을 찾다 실패했는지 "
                    "되돌려줬는지, 그래서 모델이 다른 말로 다시 시도했는지 보세요.")
        elif not hit and not calls:
            note = "검색을 한 번도 하지 않았습니다. instructions 가 시켰는데도 그렇습니다."
        metrics.judged(hit, golden.missing_keys(item, text), note)
        if not hit:
            hedged += both
            confident += bool(cited) and not both

    elapsed = time.perf_counter() - started
    empty = sum(1 for call in ctx["run"]["tool_calls"] if not call["ok"])
    headline = summarize(total, hits, confident, hedged)

    metrics.summary(
        "tool call" + (" (실패 처리 꺼짐)" if broken else ""),
        headline, ctx["run"], elapsed, hits, total,
        extra={"자신 있게 틀림": f"{confident}개 (근거를 달고 낸 오답)",
               "판단 회피": f"{hedged}개 (두 판을 나란히 적고 고르지 않음)",
               "빈손 검색": f"{empty}번"},
        next_up=("빈손 비율이 tool error rate 입니다. 이게 숫자로 나오는 이유는 실패가 "
                 "문자열이 아니라 반환값이기 때문입니다 — harness_metrics.py 의 ToolResult. "
                 "그런데 더 큰 문제는 '자신 있게 틀림' 과 '판단 회피' 쪽입니다. 둘 다 "
                 "어느 문서가 살아 있는지를 몰라서 생긴 것이고, 그건 검색으로는 알 수 "
                 "없습니다. 다음은 step 2, 그걸 아는 한 겹입니다."),
        command=("python harness/step2_verify.py --endpoint "
                 f"{args.endpoint} --questions {total}"))


if __name__ == "__main__":
    main()
