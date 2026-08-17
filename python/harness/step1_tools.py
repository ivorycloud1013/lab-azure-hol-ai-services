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

import harness_cli
import harness_metrics as metrics
import harness_tools
from golden import is_hit
from harness_metrics import ToolResult

INSTRUCTIONS = (
    "당신은 한국 주택시장 보고서 하나에 대해 답합니다. "
    "답하기 전에 반드시 먼저 검색하세요. "
    "보고하는 모든 수치에 근거를 [line N] 형식으로 다세요. N 은 그 수치가 나온 줄 번호입니다. "
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
        "description": "문서에서 여러 정규식을 한 번에 검색한다. "
                       "일치한 줄을 line:text 형식으로, 주변 맥락과 함께 돌려준다. "
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
        "description": "문서의 줄 범위를 읽는다. 검색으로 찾은 자리의 전체 맥락을 볼 때 쓴다.",
        "parameters": {
            "type": "object",
            "properties": {
                "start_line": {"type": "integer", "description": "읽기 시작할 줄, 1부터 셈"},
                "line_count": {"type": "integer",
                               "description": f"몇 줄을 읽을지, 최대 {harness_tools.MAX_READ_LINES}"},
            },
            "required": ["start_line", "line_count"],
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
    path = ctx["args"].file
    try:
        if name == "search_document":
            text, matched = harness_tools.grep(
                path, arguments["patterns"], arguments.get("context_lines",
                                                           harness_tools.CONTEXT_LINES))
            return ToolResult(text, matched)
        if name == "read_lines":
            return ToolResult(harness_tools.read(path, arguments["start_line"],
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

        response = ctx["client"].responses.create(
            model=args.deployment, previous_response_id=response.id,
            input=outputs, tools=TOOLS)
        ctx["run"] = metrics.add_usage(ctx["run"], response.usage)

    print(f"    [tool round {MAX_TOOL_ROUNDS}회를 넘겨 중단]")
    return response.output_text


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

    metrics.header("step 1 — tool call" + (" (실패 처리 OFF)" if args.broken_tools else ""),
                   f"{args.deployment} · 질문 {len(ctx['golden'])}개 · grep 은 모든 단계와 동일")

    started = time.perf_counter()
    hits = 0
    for item in ctx["golden"]:
        text = answer(ctx, item["question"])
        hit = is_hit(item, text)
        hits += hit
        print(f"  {'hit ' if hit else 'miss'} {item['id']}")

    metrics.report("tool call", ctx["run"], time.perf_counter() - started,
                   hits, len(ctx["golden"]))
    print("\n  tool error rate 는 dispatcher 가 거부한 호출의 비율입니다. 이게 숫자로 나오는")
    print("  이유는 실패가 반환값이기 때문입니다 — harness_metrics.py 의 ToolResult 참고.")
    print("  다음: step 2 는 질문 하나로 끝나지 않을 때 무슨 일이 생기는지 봅니다.")


if __name__ == "__main__":
    main()
