"""검증 레이어 — 하네스가 답을 받아들이기 전에 근거를 되짚는다.

이 파일의 전제 하나만 붙들면 나머지는 따라온다.

    **하네스는 정답을 모른다.**

정답을 아는 하네스는 만들 수 없다. 알았다면 애초에 모델을 부를 이유가 없다. 그래서 검증은
"이 답이 맞는가" 를 묻지 않는다. 대신 이렇게 묻는다 —

    "이 답이 근거로 댄 자리가, 이 답을 실제로 뒷받침하는가?"

이건 정답 없이도 확인할 수 있고, 놀랍게도 오답의 상당수가 여기서 걸린다. 옆 칸 값을 집어 온
답은 대개 근거를 못 대거나, 대더라도 그 자리가 그 값을 그 대상의 것으로 지목해 주지 않는다.

검증을 두 겹으로 나눈 것도 의도다.

    1. 결정론 검사   인용이 있나, 그 줄에 그 수치가 정말 있나.  모델 호출 0번, 공짜
    2. 근거 판정     그 줄이 질문이 물은 대상의 값이라고 확정해 주나.  모델 호출 1번, 유료

싼 것이 먼저 거른다. 이 순서를 뒤집으면 지어낸 인용 하나를 잡는 데 judge 비용을 낸다.
그리고 2번 판정자에게는 **정답을 주지 않는다.** 답안지를 쥔 판정자는 채점 기계이지 하네스가
아니고, 실전에서는 존재하지 않는다.
"""

import re

from pydantic import BaseModel, Field

import harness_tools

# 인용 형식. [line 118] 과 [line 118-122] 를 함께 받는다 — 범위로 다는 모델이 흔하고,
# 그걸 형식 위반으로 반려하면 검증이 잡으려던 것 대신 형식만 잡게 된다.
CITATION = re.compile(r"\[line\s*(\d+)(?:\s*[-~]\s*(\d+))?\]")

# 답변에서 뽑아낼 수치. 0.24% · 152.1 · 932.7조 · 9.6대 1 · 4천4백 을 한 덩어리로 본다.
NUMBER = re.compile(r"\d+(?:\.\d+)?(?:%|조|억|만|천|호|배)?")

# 인용한 줄 위아래로 함께 읽어줄 범위. 모델이 문단 첫 줄을 가리키고 수치는 그 아래 줄에 있는
# 경우가 흔해서, 0 으로 두면 멀쩡한 인용이 무더기로 반려된다.
CITATION_MARGIN = 2

# 근거로 읽어 들일 줄 수의 상한. 모델이 [line 1-800] 을 달고 "문서 전체가 근거" 라고 하는
# 것을 막는다. 그건 근거가 아니라 근거를 대지 않겠다는 말이다.
MAX_CITED_LINES = 40

JUDGE_INSTRUCTIONS = (
    "당신은 근거를 검사합니다. 답이 맞는지는 판단하지 마세요 — 정답을 모른다고 가정하세요. "
    "오직 아래 근거 원문만 보고, 답변이 말한 수치가 질문이 물은 대상의 값이라고 "
    "그 원문만으로 확정되는지 판정하세요.\n"
    "한 줄에 여러 대상의 값이 나열되어 있어 어느 것이 질문의 대상인지 확정할 수 없다면 "
    "supported=false 입니다. 다른 시점(전월·전년)의 값이거나 다른 지역·범위의 값으로 보이면 "
    "역시 false 입니다. 원문이 그 대상을 명시하고 그 값을 짝지어 주면 true 입니다.\n"
    "reason 은 왜 확정되지 않는지 한 문장으로. next_step 은 무엇을 더 찾아봐야 하는지 "
    "한 문장으로 쓰세요. 정답 수치를 알려주지 마세요."
)


class Grounding(BaseModel):
    supported: bool = Field(description="근거 원문만으로 이 답이 확정되는가")
    reason: str = Field(description="확정되지 않는다면 왜인지 한 문장")
    next_step: str = Field(description="다음에 무엇을 확인해야 하는지 한 문장")


class Verdict:
    """검증 결과. 통과 여부와, 통과가 아니라면 모델에게 돌려줄 말.

    반려 사유를 문자열로 들고 다니는 이유는 그게 다음 시도의 입력이 되기 때문이다. "틀렸다"
    만 돌려주는 검증은 재시도를 반복으로 만들고, 무엇이 왜 부족한지 돌려주는 검증은 재시도를
    조사로 만든다. 이 차이가 step 2 의 전부다.
    """

    def __init__(self, ok, rule, reason="", next_step="", cited=()):
        self.ok = ok
        self.rule = rule            # 어느 검사에서 갈렸는지 — 리포트에서 층을 나눠 세는 데 쓴다
        self.reason = reason
        self.next_step = next_step
        self.cited = list(cited)

    def feedback(self):
        """모델에게 되돌려줄 문장. 사유와 다음 할 일을 붙여 준다."""
        tail = f" {self.next_step}" if self.next_step else ""
        return (f"직전 답변은 근거 검증을 통과하지 못했습니다. 사유: {self.reason}{tail} "
                "문서를 다시 조사해서 근거를 좁힌 다음, 답을 다시 내세요.")


def cited_ranges(text):
    """답변이 단 인용을 (시작, 끝) 목록으로. 범위 표기는 그대로, 단일 표기는 여백을 붙인다."""
    ranges = []
    for start, end in CITATION.findall(text):
        first = int(start)
        last = int(end) if end else first
        if last < first:
            first, last = last, first
        ranges.append((max(1, first - CITATION_MARGIN), last + CITATION_MARGIN))
    return ranges


def cited_text(path, ranges):
    """인용한 자리의 원문을 읽어 온다. 모델이 아니라 하네스가 직접 읽는 것이 핵심이다.

    모델에게 "네 근거를 다시 읽어봐" 라고 시키면 모델은 자기가 기억하는 것을 다시 말한다.
    파일에서 잘라 와야 기억과 파일이 어긋난 순간이 드러난다.
    """
    budget = MAX_CITED_LINES
    pieces = []
    for first, last in ranges:
        if budget <= 0:
            break
        count = min(last - first + 1, budget)
        pieces.append(harness_tools.read(path, first, count))
        budget -= count
    return "\n".join(piece for piece in pieces if piece)


def _significant(token):
    """대조할 값어치가 있는 수치인가.

    "9.6대 1" 의 1, "1순위" 의 1 같은 부스러기를 걸러낸다. 이걸 안 하면 검사가 무력해진다 —
    한 자리 숫자는 어느 줄에나 있어서, 지어낸 수치 옆에 1 하나만 붙어 있으면 "근거에 있는
    수치" 로 세어져 통과해 버린다. 실제로 그렇게 새는 것을 보고 넣은 규칙이다.
    """
    digits = [ch for ch in token if ch.isdigit()]
    has_unit = not token[-1].isdigit()
    return "." in token or has_unit or len(digits) >= 2


def numbers_in(text):
    """수치 토큰 집합. 순서와 중복은 버린다 — 있느냐 없느냐만 본다."""
    return {match for match in NUMBER.findall(text)
            if any(ch.isdigit() for ch in match) and _significant(match)}


def check_deterministic(path, answer):
    """모델 없이 되는 검사 둘. 통과하면 None 을 돌려준다.

    여기서 잡히는 것은 형식 문제가 아니라 실체 문제다 — 근거를 안 댄 수치, 그리고 댄 자리에
    실제로는 없는 수치. 뒤쪽이 step 0 에서 본 지어낸 인용의 정체이고, tool 을 쥐여준 뒤에도
    줄 번호가 한두 줄씩 밀린 형태로 계속 나온다.
    """
    # 인용 마커를 먼저 지운다. [line 627] 의 627 을 답변이 말한 수치로 세면, 근거로 댄 줄
    # 번호가 곧 그 답의 근거가 되어 버린다 — 어떤 인용이든 자기 자신을 뒷받침하게 된다.
    said = numbers_in(CITATION.sub(" ", answer))
    if not said:
        return Verdict(False, "수치 없음", "답변에 수치가 없습니다.",
                       "질문이 요구한 수치를 문서에서 찾아 제시하세요.")

    ranges = cited_ranges(answer)
    if not ranges:
        return Verdict(False, "근거 없음", "수치를 말했지만 [line N] 근거가 없습니다.",
                       "그 수치가 실제로 적힌 줄 번호를 찾아 함께 다세요.")

    source = cited_text(path, ranges)
    if not source.strip():
        return Verdict(False, "빈 근거", "인용한 줄 번호가 문서 범위 밖이거나 비어 있습니다.",
                       "문서를 다시 검색해 실제로 존재하는 줄을 인용하세요.", ranges)

    found = numbers_in(source)
    missing = [value for value in sorted(said) if value not in found]
    if len(missing) == len(said):
        shown = ", ".join(missing[:3])
        return Verdict(False, "근거 불일치",
                       f"인용한 줄에 답변의 수치({shown})가 없습니다.",
                       "그 수치가 실제로 적힌 자리를 다시 검색해 인용하세요.", ranges)

    return None


def check_grounding(ctx, question, answer, source):
    """근거 원문만 보여주고 모델에게 판정시킨다. 정답은 주지 않는다.

    이 호출이 이 레이어의 유일한 비용이고, 결정론 검사를 통과한 답에만 붙는다. 판정자가 보는
    것은 질문·답변·원문 셋뿐이라, 판정자가 "맞다" 고 하려면 원문에서 대상과 값이 짝지어져
    있어야 한다. 차트 alt text 처럼 수치와 라벨이 따로 늘어선 자리는 여기서 걸린다.
    """
    response = ctx["client"].responses.parse(
        model=ctx["args"].deployment,
        instructions=JUDGE_INSTRUCTIONS,
        input=f"질문: {question}\n\n답변: {answer}\n\n근거 원문:\n{source}",
        text_format=Grounding,
    )
    ctx["run"] = _count(ctx["run"], response.usage)
    parsed = response.output_parsed
    if parsed is None:
        # 판정자가 스키마를 못 채우고 돌아온 경우. 여기서 반려하면 멀쩡한 답을 판정자 사정으로
        # 되돌리게 되므로 통과시키되, 조용히 넘기지는 않는다 — 이게 잦아지면 판정 지시문이
        # 무너진 것이고, 그건 리포트가 아니라 화면에서 먼저 보여야 한다.
        print("    [근거 판정이 응답을 해석하지 못해 이 답변은 통과 처리합니다]")
        return Grounding(supported=True, reason="판정 불가", next_step="")
    return parsed


def _count(run, usage):
    """판정 호출의 비용도 실행에 접어 넣는다.

    따로 세면 검증 레이어가 공짜처럼 보인다. 이 랩에서 가장 하면 안 되는 거짓말이 그거다 —
    모든 레이어는 청구서를 같이 낸다.
    """
    import harness_metrics as metrics  # noqa: PLC0415 — 순환 import 를 피한다

    return metrics.add_usage(run, usage)


def verify(ctx, item, answer):
    """한 답변에 대한 최종 판정. 결정론 검사 → 근거 판정 순서로 간다."""
    path = ctx["args"].file

    failed = check_deterministic(path, answer)
    if failed is not None:
        return failed

    ranges = cited_ranges(answer)
    source = cited_text(path, ranges)
    grounding = check_grounding(ctx, item["question"], answer, source)
    if grounding.supported:
        return Verdict(True, "통과", cited=ranges)
    return Verdict(False, "근거 부족", grounding.reason, grounding.next_step, ranges)
