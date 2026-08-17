"""하네스 랩 공용 계측과 출력. 모든 단계가 같은 형식으로 말한다.

출력에 대한 원칙 하나 — 이 랩의 로그는 사람이 읽고 배우라고 있는 것이지 대시보드가 아니다.
그래서 어떤 단계도 "miss sale-price-nationwide" 같은 줄을 찍지 않는다. 무엇을 물었고, 뭐라고
답했고, 왜 틀렸는지가 같이 나와야 그 줄을 보고 뭔가를 알 수 있다.

계측 쪽에서 눈여겨볼 것 하나 — tool 결과가 실패 여부를 값으로 들고 다닌다(ToolResult.ok).
텍스트에서 짐작하지 않는다. 실패가 "no match 로 시작하는 문자열" 일 뿐이면 tool error rate 를
계산할 방법이 없다. 실패를 일급 반환값으로 만드는 것 자체가 하네스를 짓는 일의 일부다.
"""

import unicodedata
from collections import namedtuple

WIDTH = 76
THICK = "═" * WIDTH
THIN = "─" * WIDTH

# text 는 모델에게 돌아가고, ok 는 계측용이라 모델에게 가지 않는다.
ToolResult = namedtuple("ToolResult", "text ok")


# ─────────────────────────────────────────────────────────────── 계측 ──


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


# ─────────────────────────────────────────────────────────────── 출력 ──


def _cells(text):
    """터미널에서 이 문자열이 차지하는 칸 수.

    한글·한자·가나는 한 글자가 두 칸을 먹는다. len() 으로 정렬하면 한글이 섞인 순간
    표가 어긋나고, textwrap 으로 접으면 줄이 화면 밖으로 나간다. 이 랩의 출력은 거의 다
    한글이라 이 함수 없이는 어떤 정렬도 맞지 않는다.
    """
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in str(text))


def _pad(text, cells):
    """칸 수 기준으로 오른쪽을 채운다. f-string 의 :<16 은 글자 수로 세서 못 쓴다."""
    return str(text) + " " * max(0, cells - _cells(text))


def _wrap(text, indent):
    """터미널 폭에 맞춰 접는다. 한 줄이 화면을 넘어가면 사람이 안 읽는다.

    textwrap 을 쓰지 않는 이유는 그쪽이 글자 수로 세기 때문이다. 한글 76자는 152칸이라
    거의 두 배로 삐져나간다.
    """
    prefix = " " * indent
    limit = WIDTH - indent
    lines, current = [], ""
    for word in " ".join(str(text).split()).split(" "):
        candidate = f"{current} {word}".strip()
        if current and _cells(candidate) > limit:
            lines.append(prefix + current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(prefix + current)
    return "\n".join(lines)


def _listed(items):
    """값 목록을 조사 없이 붙인다. '0.08% 이(가)' 같은 문장을 안 쓰기 위한 것."""
    return ", ".join(str(item) for item in items)


def header(step, name, tagline):
    """단계의 첫 줄. 'step 0 · baseline — 하네스 없이 모델만' 처럼 읽힌다."""
    print(f"\n{THICK}")
    print(f" step {step} · {name} — {tagline}")
    print(THICK)


def overview(does, watch, facts):
    """단계가 시작되기 전에 무엇을 볼 것인지 알려준다.

    이게 없으면 학습자는 숫자가 다 지나간 뒤에야 무엇을 봤어야 했는지 알게 된다. 그때는
    이미 늦었고, 다시 돌리려면 돈이 든다.
    """
    print("\n 무엇을 하나")
    print(_wrap(does, 3))
    print("\n 무엇을 볼까")
    print(_wrap(watch, 3))
    if facts:
        print()
        for label, value in facts:
            print(f" {_pad(label, 10)}{_wrap(value, 0)}" if _cells(value) + 11 <= WIDTH
                  else f" {_pad(label, 10)}{value}")
    print()


def case(index, total, question):
    """질문 하나가 시작된다. 번호와 질문 전문을 같이 보여준다 — id 만으로는 아무것도 모른다."""
    print(THIN)
    tag = f" [{index}/{total}] "
    body = _wrap(question, 0).splitlines() or [""]
    print(tag + body[0])
    for line in body[1:]:
        print(" " * len(tag) + line)


def _field(label, text):
    """'   답변  …' 처럼 라벨을 붙이고, 이어지는 줄은 본문에 맞춰 들여쓴다."""
    body = _wrap(text, 0).splitlines() or [""]
    print(f"   {_pad(label, 6)}{body[0]}")
    for line in body[1:]:
        print(f"         {line}")


def said(text, limit=220):
    """모델이 뭐라고 했는지. 판정만 찍고 답을 감추면 왜 그런 판정인지 알 수가 없다."""
    body = " ".join(str(text).split())
    if len(body) > limit:
        body = body[:limit].rstrip() + " …"
    print()
    _field("답변", body or "(빈 응답)")


def judged(hit, missing=None, note=None):
    """맞았는지, 아니면 무엇이 없어서 틀렸는지.

    빠진 값을 조사 없이 목록으로 붙인다. "0.08% 이(가) 없음" 은 코드가 한국어를 흉내 내다
    실패한 문장이고, 읽는 사람에게 아무것도 더해주지 않는다.
    """
    if hit:
        _field("판정", "hit")
    elif missing:
        _field("판정", f"miss — 답변에 없는 값: {_listed(missing)}")
    else:
        _field("판정", "miss")
    if note:
        _field("메모", note)


def used(calls):
    """이 질문에 쓴 tool call 을 한 줄로 요약한다."""
    if not calls:
        return
    failed = sum(1 for call in calls if not call["ok"])
    tail = f" (그중 {failed}번은 빈손)" if failed else ""
    _field("조사", f"tool call {len(calls)}번{tail}")


def _stat(label, value):
    print(f" {_pad(label, 16)}{value}")


def _percent(value):
    return "n/a" if value is None else f"{value * 100:.0f}%"


def summary(name, headline, run, seconds, hits=None, total=None, extra=None,
            next_up=None, command=None):
    """실행이 끝나고 나오는 블록. 숫자 앞에 그 숫자를 요약하는 문장이 먼저 온다.

    표만 던지면 학습자는 어느 칸을 봐야 하는지 모른다. headline 이 그걸 대신 말해 준다.
    """
    print(f"\n{THICK}")
    print(f" 결과 — {name}")
    print(THICK)
    print()
    print(_wrap(headline, 1))
    print()

    if total:
        _stat("hit", f"{hits}/{total}")
    _stat("model 호출", f"{run['turns']}번")

    calls = run["tool_calls"]
    if calls:
        failed = sum(1 for call in calls if not call["ok"])
        _stat("tool call", f"{len(calls)}번 (빈손 {failed}번, {_percent(error_rate(run))})")
        repeats = redundant_work(run)
        if repeats:
            _stat("같은 호출 반복", f"{repeats}번")

    cached = cache_rate(run)
    cache_note = f" (cache 재사용 {_percent(cached)})" if cached else ""
    _stat("input token", f"{run['input_tokens']:,}{cache_note}")
    _stat("output token", f"{run['output_tokens']:,}")

    slope = growth_slope(run["turn_tokens"])
    if slope is not None:
        direction = "늘어남" if slope > 0 else "평평함"
        _stat("호출당 증가", f"{slope:+,.0f} token — {direction}")

    _stat("걸린 시간", f"{seconds:.1f}초")

    for label, value in (extra or {}).items():
        _stat(label, value)

    if next_up:
        print()
        print(_wrap(next_up, 1))
    if command:
        # 명령은 접지 않는다. 줄이 접히면 붙여넣었을 때 그대로 돌지 않는다.
        print(f"\n   {command}")
    print()
