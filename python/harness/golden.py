"""모든 단계를 재는 질문 세트와, 그 질문들이 나온 코퍼스.

공용으로 두는 이유는 step 0 과 step 2 가 똑같은 질문에 답하게 하기 위해서다. 단계마다 자기
예제를 들고 있으면, 단계 사이에서 숫자가 움직였을 때 그게 질문이 바뀐 탓일 가능성이 늘
남는다. 그러면 이 랩의 어떤 숫자도 무엇의 근거도 되지 못한다.

질문을 고른 기준이 이 랩의 전부다. **답이 어느 edition 을 봤느냐에 따라 갈리는 질문**만 골랐다.

코퍼스에는 같은 보고서의 edition 이 둘 들어 있다. 하나는 이미 대체된 구판이고, 하나는 수정본이다.
둘은 줄 번호까지 같고 수치 몇 개만 다르다. 문서 어디에도 "이건 구판이다" 라는 표시가 없다 —
실무의 문서가 그렇기 때문이다. 파일이 인덱스에 두 번 들어간 것뿐이고, 문서는 자기가 대체된
줄 모른다. 검색은 둘 다 물어오고, 모델은 어느 쪽이 살아 있는지 판단할 근거가 없다.

그 판단은 **하네스의 몫**이고, 그래서 이 랩의 step 1 은 틀린다. 아래 EDITIONS 가 그 지식이며,
step 2 부터만 읽는다. step 1 이 이 표를 읽지 않는다는 것이 "step 1 에는 edition 개념이 없다" 의
코드상 의미다.

lure 는 구판 값이다. 화면에 "아, 저기로 빠졌구나" 를 띄우는 데 쓰고, **채점에도 쓴다** —
왜 쓰는지는 is_hit 에 적었다.
"""

import os
import re

PYTHON_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CORPUS = os.path.join(PYTHON_DIR, "assets", "tools", "harness")

# 하네스가 아는 문서 관리 메타데이터. 정답이 아니라 **어느 문서가 살아 있는가** 다.
# 문서 안에서는 알아낼 수 없어서 여기 적는다 — 구판은 자기가 대체된 줄 모른다.
# 이 표에 없는 것은 하네스도 모른다. 그래서 하네스는 여전히 정답을 모른다.
CURRENT_EDITION = "KB-2510-8092"
SUPERSEDED_EDITIONS = {"KB-2510-4471": CURRENT_EDITION}

# 함정이 있는 것부터 놓았다. --questions 3 으로 앞의 셋만 돌려도 이 랩이 하려는 말이 나오도록.
# 정답은 수정본 값, lure 는 구판 값이다. 둘 다 문서에 literal 로 있어야 검증이 대조할 수 있다.
GOLDEN = [
    {
        # 구판 409.2 → 수정본 386.5. 이 수치는 본문에 없고 그림 18 안에만 있어서, 검색하면
        # 두 edition 의 그림이 나란히 돌아온다. 값이 다르다는 것은 보이지만 어느 쪽이 유효한지는
        # 어디에도 없다.
        "id": "seoul-subscription",
        "question": "9월 서울 아파트 1순위 청약 경쟁률은 얼마인가요?",
        "answer_key": ["386.5"],
        "lure": ["409.2"],
        "lure_why": "409.2 는 구판 KB-2510-4471 의 수치이고, 수정본에서 바뀌었다",
    },
    {
        # 구판 0.24% → 수정본 0.21%. 이 값은 요약(73줄)과 본문(120줄) 두 군데에 있어서
        # 검색 한 번에 네 줄(두 edition × 두 자리)이 돌아온다. 가장 헷갈리는 모양이다.
        "id": "capital-sale-price",
        "question": "9월 수도권 주택 매매가격은 전월 대비 몇 % 상승했나요?",
        "answer_key": ["0.21%"],
        "lure": ["0.24%"],
        "lure_why": "0.24% 는 구판 KB-2510-4471 의 수치이고, 수정본에서 바뀌었다",
    },
    {
        # 구판 152.1 → 수정본 149.7. 한 줄에만 있어서 두 edition 의 차이가 가장 또렷하게 보인다.
        # 그런데도 모델은 둘 중 하나를 골라야 하고, 고를 근거가 없다.
        "id": "jeonse-supply-index",
        "question": "9월 전국 전세수급지수는 얼마이고, 언제 이후 최고치인가요?",
        "answer_key": ["149.7", "2021년 10월"],
        "lure": ["152.1"],
        "lure_why": "152.1 은 구판 KB-2510-4471 의 수치이고, 수정본에서 바뀌었다",
    },
    {
        # 여기부터 대조군. 두 edition 에서 값이 같아 하네스가 잡을 것이 없다. 대조군이 있어야
        # 검증 레이어가 멀쩡한 답까지 붙잡고 늘어지는지(= 헛수고) 를 볼 수 있다.
        "id": "sale-price-nationwide",
        "question": "2025년 9월 전국 주택 매매가격은 전월 대비 몇 % 상승했나요?",
        "answer_key": ["0.08%"],
        "lure": [],
        "lure_why": "",
    },
    {
        "id": "monthly-rent-share",
        "question": "8월 전국 주택 전월세 거래에서 월세 비중은 얼마인가요? 수도권과 비수도권도 알려주세요.",
        "answer_key": ["66.0%", "64.4%", "69.2%"],
        "lure": [],
        "lure_why": "",
    },
    {
        "id": "mortgage-balance",
        "question": "9월 은행권 주택담보대출 잔액은 얼마이고, 전월 대비 증가액은 얼마인가요?",
        "answer_key": ["932.7조", "2조 5천억"],
        "lure": [],
        "lure_why": "",
    },
]


def load_corpus(path):
    """코퍼스를 한 번만 읽고, 문서 이름 → (경로, 줄 목록) 으로 돌려준다.

    각 단계가 질문마다 여러 번 읽는다. 호출할 때마다 읽으면 디스크 시간이 seconds 컬럼에
    섞여 들어가는데, 그 컬럼은 하네스만 재라고 있는 것이다.

    이름순으로 정렬한다. 검색 결과에 문서가 돌아오는 순서가 실행마다 달라지면 두 실행을
    나란히 놓을 수 없다.
    """
    corpus = {}
    for name in sorted(os.listdir(path)):
        if not name.endswith(".md"):
            continue
        full = os.path.join(path, name)
        with open(full, encoding="utf-8") as handle:
            corpus[os.path.splitext(name)[0]] = {
                "path": full,
                "lines": handle.read().splitlines(),
            }
    return corpus


def is_superseded(document):
    """이 문서가 대체된 edition 인가. 하네스만 답할 수 있는 질문이다."""
    return document in SUPERSEDED_EDITIONS


def questions(count=None):
    """앞에서부터 count 개. 함정 문항이 앞에 있어서 --questions 3 도 이 랩을 말한다."""
    return GOLDEN[:count]


def is_hit(item, text):
    """정답 문자열이 전부 들어 있고, 구판 값은 들어 있지 않아야 한다.

    뒤 조건이 이 랩에서 새로 붙은 것이다. "구판은 409.2, 수정본은 386.5 입니다" 는 답이
    아니라 판단을 사용자에게 떠넘긴 것이고, 그걸 hit 으로 세면 이 랩이 재려는 실패가 통계에서
    사라진다. 어느 edition 이 유효한지 고르는 것이 여기서 요구하는 일이다.

    모델을 안 쓰니 공짜이고, 몇 번을 돌려도 같은 답이 나온다. 그래서 모든 리포트의 첫 줄이
    이 숫자다.
    """
    return (all(key in text for key in item["answer_key"])
            and not lured(item, text))


def missing_keys(item, text):
    """답변에서 빠진 정답 문자열. miss 를 찍을 때 무엇이 없어서 miss 인지 같이 말해준다.

    "miss" 만 찍으면 학습자는 답을 눈으로 훑으며 뭐가 틀렸는지 직접 찾아야 한다. 세 값 중
    둘은 맞고 하나만 틀린 경우가 특히 그런데, 그건 정답에 가까운 실패라 오히려 볼 값어치가 있다.
    """
    return [key for key in item["answer_key"] if key not in text]


def lured(item, text):
    """답변이 구판 값을 집어 왔는가.

    miss 를 찍을 때 "그냥 틀렸다" 와 "대체된 문서에서 가져왔다" 는 아주 다른 실패다.
    뒤쪽이 이 랩이 다루는 실패이고, 검증 레이어가 잡으라고 만든 실패이기도 하다.
    """
    return [value for value in item.get("lure", []) if value in text]


def citations(text):
    """답변이 단 [문서 line N] 인용. (문서, 줄) 목록으로 돌려준다.

    step 0 에서 지어낸 인용을 세고, step 2 에서 폐기된 edition 을 인용했는지 보는 데 쓴다.
    """
    return [(document, int(number))
            for document, number in re.findall(r"\[([\w.-]+)\s+line\s*(\d+)", text)]
