"""하네스 랩 공용 계측. 모든 단계가 똑같은 리포트 블록을 찍는다.

단계마다 출력 형식이 다르면 어떤 숫자를 단계 사이로 따라갈 수가 없다. 그런데 숫자 하나를
단계 사이로 따라가는 것이 레이어가 무슨 일을 했는지 보는 유일한 방법이라, 형식은 여기서
고정하고 어떤 단계도 자기 형식을 갖지 못하게 한다.

이 파일에서 눈여겨볼 것 하나 — tool 결과가 실패 여부를 값으로 들고 다닌다(ToolResult.ok).
텍스트에서 짐작하지 않는다. 실패가 "no match 로 시작하는 문자열" 일 뿐이면 tool error rate 를
계산할 방법이 없다. 실패를 일급 반환값으로 만드는 것 자체가 하네스를 짓는 일의 일부다.
"""

from collections import namedtuple

BANNER = "=" * 72
RULE = "-" * 72

# text 는 모델에게 돌아가고, ok 는 계측용이라 모델에게 가지 않는다.
ToolResult = namedtuple("ToolResult", "text ok")


def new_run():
    """단계가 쌓아 올리는 카운터들.

    turns 는 대화 턴이 아니라 model 호출 횟수다. token 컬럼이 이 값으로 나누기 때문에,
    둘을 섞으면 tokens/turn 이 슬그머니 아무 의미도 없는 숫자가 된다.
    """
    return {
        "text": "",
        "response_id": None,
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_tokens": 0,
        "turns": 0,
        "turn_tokens": [],   # model 호출마다의 input token, 순서대로 — 증가 곡선
        "tool_calls": [],    # {"name", "arguments", "ok"} 순서대로
    }


def add_usage(run, usage):
    """응답 하나의 usage 를 접어 넣은 새 run 을 돌려준다.

    cached_tokens 는 한 겹 안에 있고 캐시를 지원하지 않는 배포에는 아예 없어서 방어적으로
    읽는다. 여기서 속성 하나 없다고 실행이 끝나면, 리포트에서 가장 덜 중요한 숫자 때문에
    나머지를 다 잃는 셈이 된다.
    """
    details = getattr(usage, "input_tokens_details", None)
    cached = getattr(details, "cached_tokens", 0) or 0 if details is not None else 0
    tokens = usage.input_tokens or 0
    return {
        **run,
        "input_tokens": run["input_tokens"] + tokens,
        "output_tokens": run["output_tokens"] + (usage.output_tokens or 0),
        "cached_tokens": run["cached_tokens"] + cached,
        "turns": run["turns"] + 1,
        "turn_tokens": run["turn_tokens"] + [tokens],
    }


def record_tool_call(run, name, arguments, result):
    """tool call 하나를 기록한다. arguments 까지 남겨야 redundant_work 가 반복을 알아본다."""
    entry = {"name": name, "arguments": arguments, "ok": result.ok}
    return {**run, "tool_calls": run["tool_calls"] + [entry]}


def merge(first, second):
    """run 두 개를 더한다. text 와 response_id 는 나중 것을 쓴다.

    질문을 여러 개 던지는 단계는 질문마다 run 이 하나씩 필요하고 합계도 필요하다. 그 합을
    단계마다 따로 짜지 않고 여기서 하는 것이, 어디서나 리포트가 같게 나오는 이유다.
    """
    return {
        "text": second["text"] or first["text"],
        "response_id": second["response_id"] or first["response_id"],
        "input_tokens": first["input_tokens"] + second["input_tokens"],
        "output_tokens": first["output_tokens"] + second["output_tokens"],
        "cached_tokens": first["cached_tokens"] + second["cached_tokens"],
        "turns": first["turns"] + second["turns"],
        "turn_tokens": first["turn_tokens"] + second["turn_tokens"],
        "tool_calls": first["tool_calls"] + second["tool_calls"],
    }


def error_rate(run):
    """dispatcher 가 거부한 tool call 의 비율. 아무것도 안 불렀으면 None."""
    calls = run["tool_calls"]
    if not calls:
        return None
    return sum(1 for call in calls if not call["ok"]) / len(calls)


def redundant_work(run):
    """앞선 호출과 완전히 똑같은 tool call 이 몇 번이나 있었나.

    반복은 첫 결과에서 아무것도 못 배운 agent 의 서명이다. tool 이 쓸모 있는 말을 안 해줬거나,
    알아낸 것을 남겨둘 데가 없었거나 둘 중 하나다. step 1 과 step 3 이 이 숫자를 움직이는데,
    바로 그 서로 다른 두 이유 때문이다.
    """
    seen, repeats = set(), 0
    for call in run["tool_calls"]:
        key = (call["name"], repr(sorted(call["arguments"].items())))
        if key in seen:
            repeats += 1
        seen.add(key)
    return repeats


def growth_slope(turn_tokens):
    """호출 번호에 대한 input token 의 최소제곱 기울기. 단위는 호출당 token.

    context 관리의 대표 숫자다. 히스토리를 통째로 들고 가는 대화는 올라가고, 압축하거나
    회수하는 대화는 평평해진다. 총합이 아니라 기울기를 보는 이유는, 길이가 다른 두 실행을
    나란히 놓고 비교할 수 있게 하기 위해서다.
    """
    count = len(turn_tokens)
    if count < 2:
        return None
    mean_x = (count - 1) / 2
    mean_y = sum(turn_tokens) / count
    variance = sum((x - mean_x) ** 2 for x in range(count))
    if not variance:
        return None
    covariance = sum((x - mean_x) * (y - mean_y) for x, y in enumerate(turn_tokens))
    return covariance / variance


def cache_rate(run):
    return run["cached_tokens"] / run["input_tokens"] if run["input_tokens"] else None


def _line(label, value):
    print(f"  {label:<18}{value}")


def _ratio(value):
    return "n/a" if value is None else f"{value * 100:.1f}%"


def report(layer, run, seconds, hits=None, total=None, extra=None):
    """모든 단계가 찍는 블록. 같은 라벨, 같은 순서, 언제나."""
    print(f"\n{RULE}")
    _line("layer", layer)
    if total:
        _line("hit", f"{hits}/{total}")
    _line("turns", run["turns"])

    calls = run["tool_calls"]
    if calls:
        failures = sum(1 for call in calls if not call["ok"])
        _line("tool calls", f"{len(calls)}  (errors {failures}, {_ratio(error_rate(run))})")
        _line("redundant calls", redundant_work(run))
    else:
        _line("tool calls", "0")

    _line("input tokens", f"{run['input_tokens']:,}  (cached {_ratio(cache_rate(run))})")
    _line("output tokens", f"{run['output_tokens']:,}")
    if run["turns"]:
        _line("tokens/turn", f"{run['input_tokens'] // run['turns']:,}")
    slope = growth_slope(run["turn_tokens"])
    if slope is not None:
        _line("context growth", f"{slope:+,.0f} tokens/call")
    _line("seconds", f"{seconds:.1f}")

    for label, value in (extra or {}).items():
        _line(label, value)
    print(RULE)


def header(title, subtitle=None):
    print(f"\n{BANNER}")
    print(title)
    if subtitle:
        print(subtitle)
    print(BANNER)
