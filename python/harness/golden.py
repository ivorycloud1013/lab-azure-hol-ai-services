"""모든 단계를 재는 질문 세트와, 그 질문들이 나온 문서.

공용으로 두는 이유는 step 0 과 step 5 가 똑같은 질문에 답하게 하기 위해서다. 단계마다 자기
예제를 들고 있으면, 단계 사이에서 숫자가 움직였을 때 그게 질문이 바뀐 탓일 가능성이 늘
남는다. 그러면 이 랩의 어떤 숫자도 무엇의 근거도 되지 못한다.
"""

import os
import re

PYTHON_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DOCUMENT = os.path.join(PYTHON_DIR, "assets", "tools", "KB주택시장리뷰_2025년 10월호.md")

# 답이 보고서에 그대로 찍혀 있는 수치인 질문만 골랐다. 틀린 답은 대놓고 틀려서 심판이 없어도
# 보인다. source_lines 는 답이 있는 자리이고, 그 줄의 본문은 실행 시점에 파일에서 잘라온다.
GOLDEN = [
    {
        "id": "sale-price-nationwide",
        "question": "2025년 9월 전국 주택 매매가격은 전월 대비 몇 % 상승했나요?",
        "answer_key": ["0.08%"],
        "source_lines": [(118, 122)],
    },
    {
        "id": "monthly-rent-share",
        "question": "8월 전국 주택 전월세 거래에서 월세 비중은 얼마인가요? 수도권과 비수도권도 알려주세요.",
        "answer_key": ["66.0%", "64.4%", "69.2%"],
        "source_lines": [(496, 500)],
    },
    {
        "id": "unsold-apartments",
        "question": "8월 전국 미분양 아파트는 몇 호이고, 전월 대비 얼마나 늘었나요?",
        "answer_key": ["6.6만", "4천4백"],
        "source_lines": [(647, 651)],
    },
    {
        "id": "mortgage-balance",
        "question": "9월 은행권 주택담보대출 잔액은 얼마이고, 전월 대비 증가액은 얼마인가요?",
        "answer_key": ["932.7조", "2조 5천억"],
        "source_lines": [(709, 713)],
    },
    {
        # 서울 수치는 본문에 없다. 차트의 alt text 와 그 아래 bullet, 그리고 차트 JSON 안에만
        # 있다. 본문만 훑으면 놓치고, 검색하면 찾는다. 그 간극 때문에 이 질문을 넣었다.
        "id": "subscription-competition",
        "question": "9월 전국 아파트 1순위 청약 경쟁률은 얼마인가요? 서울은 얼마인가요?",
        "answer_key": ["9.6대 1", "409.2"],
        "source_lines": [(608, 612), (626, 632)],
    },
    {
        "id": "jeonse-supply-index",
        "question": "9월 전국 전세수급지수는 얼마이고, 언제 이후 최고치인가요?",
        "answer_key": ["152.1", "2021년 10월"],
        "source_lines": [(535, 539)],
    },
]


def load_document(path):
    """파일을 한 번만 읽고, 쓰이는 두 형태를 함께 돌려준다.

    각 단계가 질문마다 여러 번 읽는다. 호출할 때마다 읽으면 디스크 시간이 seconds 컬럼에
    섞여 들어가는데, 그 컬럼은 하네스만 재라고 있는 것이다.
    """
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    return text, text.splitlines()


def resolve_golden(lines, count=None):
    """근거 문단을 여기 적어두지 않고 문서에서 잘라온다.

    베껴 두면 읽기는 좋고 조용히 틀린다. 문서를 다시 추출하는 순간 사본이 어긋나고, 그때부터
    step 5 는 agent 가 뒤지고 있는 파일에 있지도 않은 문장을 기준으로 채점한다. 모든 단계가
    계속 숫자를 찍고, 그 숫자가 전부 거짓말이 된다.
    """
    resolved = []
    for item in GOLDEN[:count]:
        excerpt = []
        for first, last in item["source_lines"]:
            excerpt += [f"{n}: {lines[n - 1]}" for n in range(first, last + 1) if n <= len(lines)]
        resolved.append({**item, "context": "\n".join(excerpt)})
    return resolved


def is_hit(item, text):
    """정답 문자열이 전부 들어 있어야 한다. 모델을 안 쓰니 공짜이고, 몇 번을 돌려도 같은 답이
    나온다. 그래서 모든 리포트의 첫 줄이 이 숫자다."""
    return all(key in text for key in item["answer_key"])


def missing_keys(item, text):
    """답변에서 빠진 정답 문자열. miss 를 찍을 때 무엇이 없어서 miss 인지 같이 말해준다.

    "miss" 만 찍으면 학습자는 답을 눈으로 훑으며 뭐가 틀렸는지 직접 찾아야 한다. 세 값 중
    둘은 맞고 하나만 틀린 경우가 특히 그런데, 그건 정답에 가까운 실패라 오히려 볼 값어치가 있다.
    """
    return [key for key in item["answer_key"] if key not in text]


def citations(text):
    """답변이 단 [line N] 인용 번호. step 0 에서 지어낸 인용을 세는 데 쓴다."""
    return re.findall(r"\[line\s*(\d+)\]", text)
