"""tool call loop — step 1 이 다 짓고 난 모습 그대로.

step 2 부터 5 는 이걸 다시 짓지 않고 가져다 쓴다. 이 파일이 있는 이유가 그것뿐이다. loop 는
한 번 지어봤으면 됐고, 뒤 단계가 그걸 또 가르치면 학습자가 이미 짜본 보일러플레이트 아래에
정작 그 단계의 레이어가 파묻힌다.

step1_tools.py 를 먼저 읽으세요. 여기 있는 것은 거기서 한 조각씩 조립한 결과물입니다.
"""

import json

import harness_metrics as metrics

# 모델이 tool 을 더 달라고 몇 번까지 돌아올 수 있는지의 상한. 상한이 없으면 헤매는 agent 가
# 예산이나 인내심이 바닥날 때까지 돈다. 이 랩에서 가장 값싼 guardrail 이고, 사람들이 가장
# 자주 빼먹는 것이기도 하다.
MAX_TOOL_ROUNDS = 8


def collect_tool_calls(ctx, response, dispatch):
    """모델이 요청한 tool 을 전부 실행한다. 요청이 없으면 ([], run) 을 돌려준다.

    결과는 call_id 로 짝지어 function_call_output 으로 되돌아간다. 하나라도 빠뜨리면 모델은
    영영 오지 않을 답을 기다리게 되고, loop 가 그냥 끝나는 게 아니라 다음 요청이 실패한다.
    """
    run = ctx["run"]
    outputs = []
    for item in response.output:
        if item.type != "function_call":
            continue
        arguments = json.loads(item.arguments)
        result = dispatch(ctx, item.name, arguments)
        run = metrics.record_tool_call(run, item.name, arguments, result)
        if ctx["args"].show_tools:
            marker = "  " if result.ok else "! "
            print(f"    {marker}{item.name} {json.dumps(arguments, ensure_ascii=False)}")
        outputs.append({
            "type": "function_call_output",
            "call_id": item.call_id,
            "output": result.text,
        })
    return outputs, run


def run_turn(ctx, prompt, tools, dispatch, instructions=None, previous_id=None):
    """prompt 하나를 최종 답까지 몰고 가고, 비용은 ctx['run'] 에 접어 넣는다.

    새로 시작하지 않고 run 을 넘겨받는 이유는, 여러 turn 을 한 측정 안에 넣을 수 있게 하기
    위해서다. step 2 가 context 전략에서, step 4 가 계획 실행에서 그렇게 쓴다.
    """
    args = ctx["args"]
    request = {"model": args.deployment, "input": prompt}
    if previous_id:
        # previous_response_id 가 히스토리를 서버 쪽에 들고 있어서, 새 turn 만 올라간다.
        # token 수가 더 이상 뻔하지 않게 되는 이유이기도 하다 — step 2 참고.
        request["previous_response_id"] = previous_id
    elif instructions:
        request["instructions"] = instructions
    if tools:
        request["tools"] = tools

    response = ctx["client"].responses.create(**request)
    ctx["run"] = metrics.add_usage(ctx["run"], response.usage)

    for _ in range(MAX_TOOL_ROUNDS):
        if not tools:
            break
        outputs, run = collect_tool_calls(ctx, response, dispatch)
        ctx["run"] = run
        if not outputs:
            break
        response = ctx["client"].responses.create(
            model=args.deployment, previous_response_id=response.id,
            input=outputs, tools=tools)
        ctx["run"] = metrics.add_usage(ctx["run"], response.usage)
    else:
        # round 를 다 쓰고도 호출이 남았다. 치명적이지 않다 — 헤매는 agent 도 측정값이고,
        # 여기서 예외를 던지면 이미 값을 치른 앞선 질문들까지 전부 버리게 된다.
        print(f"    [tool round {MAX_TOOL_ROUNDS}회를 넘겨 중단]")

    ctx["run"] = {**ctx["run"], "text": response.output_text, "response_id": response.id}
    return response.output_text, response.id
