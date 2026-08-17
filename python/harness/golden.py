"""모든 단계를 재는 질문 세트와, 그 질문들이 나온 문서.

공용으로 두는 이유는 step 0 과 step 3 이 똑같은 질문에 답하게 하기 위해서다. 단계마다 자기
예제를 들고 있으면, 단계 사이에서 숫자가 움직였을 때 그게 질문이 바뀐 탓일 가능성이 늘
남는다. 그러면 이 랩의 어떤 숫자도 무엇의 근거도 되지 못한다.

질문을 고른 기준이 이 랩의 전부다. **검색만 하면 그럴듯한 오답이 먼저 잡히는 질문**만 골랐다.
찾기 어려운 질문이 아니라, 찾기는 쉬운데 *엉뚱한 것을 찾기가 더 쉬운* 질문이다. 보고서는
같은 문단에 전국·수도권·서울 값을 나란히 적고, 차트는 수치와 지역명을 따로 늘어놓는다.
사람이 급히 읽어도 틀리는 자리이고, 모델은 거기서 자신 있게 틀린다.

lure 는 그 오답 후보다. 채점에는 쓰지 않는다 — 학습자가 "아, 저기로 빠졌구나" 를 바로
알아보라고 화면에 띄우는 데만 쓴다.
"""

import os
import re

PYTHON_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DOCUMENT = os.path.join(PYTHON_DIR, "assets", "tools", "KB주택시장리뷰_2025년 10월호.md")

# 함정이 센 것부터 놓았다. --questions 2 로 앞의 둘만 돌려도 이 랩이 하려는 말이 나오도록.
# source_lines 는 답이 있는 자리이고, 그 줄의 본문은 실행 시점에 파일에서 잘라온다.
GOLDEN = [
    {
        # 서울 값은 본문에 없다. 차트 alt text 는 "409.2 27.4 9.6 … 서울 강원 전국" 처럼
        # 수치 줄과 라벨 줄이 따로 있어서, 그 줄만 보고는 어느 수치가 서울인지 확정되지 않는다.
        # 게다가 검색하면 본문의 "전국 9.6대 1" 이 먼저 잡힌다. 오답으로 가는 길이 두 개다.
        "id": "seoul-subscription",
        "question": "9월 서울 아파트 1순위 청약 경쟁률은 얼마인가요?",
        "answer_key": ["409.2"],
        "source_lines": [(625, 632)],
        "lure": ["9.6", "27.4", "7.4"],
        "lure_why": "9.6 은 전국, 27.4 는 강원, 7.4 는 전월 전국 값이다",
    },
    {
        # 한 문단 안에 전국 0.08, 서울 0.52, 경기 0.11, 그리고 수도권의 8월 0.18 → 9월 0.24 가
        # 전부 들어 있다. 검색 결과를 대충 읽으면 넷 중 아무거나 집어 온다.
        "id": "capital-sale-price",
        "question": "9월 수도권 주택 매매가격은 전월 대비 몇 % 상승했나요?",
        "answer_key": ["0.24%"],
        "source_lines": [(118, 128)],
        "lure": ["0.08%", "0.52%", "0.18%", "0.11%"],
        "lure_why": "0.08 은 전국, 0.52 는 서울, 0.11 은 경기, 0.18 은 같은 수도권이지만 8월 값이다",
    },
    {
        # 본문은 152.1 인데, 바로 아래 차트 JSON 의 data 배열에는 눈금에 맞춘 155 가 들어 있다.
        # 검색이 JSON 줄을 물어오면 그럴듯한 오답이 하나 더 생긴다.
        "id": "jeonse-supply-index",
        "question": "9월 전국 전세수급지수는 얼마이고, 언제 이후 최고치인가요?",
        "answer_key": ["152.1", "2021년 10월"],
        "source_lines": [(535, 540)],
        "lure": ["155", "150"],
        "lure_why": "155·150 은 차트 데이터의 근사값이지 보고서가 말한 지수가 아니다",
    },
    {
        # 함정이 없는 문항. 대조군으로 하나는 있어야, 검증 레이어가 멀쩡한 답까지 붙잡고
        # 늘어지는지(= 헛수고) 를 볼 수 있다.
        "id": "sale-price-nationwide",
        "question": "2025년 9월 전국 주택 매매가격은 전월 대비 몇 % 상승했나요?",
        "answer_key": ["0.08%"],
        "source_lines": [(118, 122)],
        "lure": [],
        "lure_why": "",
    },
    {
        "id": "monthly-rent-share",
        "question": "8월 전국 주택 전월세 거래에서 월세 비중은 얼마인가요? 수도권과 비수도권도 알려주세요.",
        "answer_key": ["66.0%", "64.4%", "69.2%"],
        "source_lines": [(496, 500)],
        "lure": [],
        "lure_why": "",
    },
    {
        "id": "mortgage-balance",
        "question": "9월 은행권 주택담보대출 잔액은 얼마이고, 전월 대비 증가액은 얼마인가요?",
        "answer_key": ["932.7조", "2조 5천억"],
        "source_lines": [(709, 713)],
        "lure": [],
        "lure_why": "",
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


def lured(item, text):
    """답변이 이 질문의 오답 후보 중 무엇을 집어 왔는지.

    miss 를 찍을 때 "그냥 틀렸다" 와 "옆 칸 값을 가져왔다" 는 아주 다른 실패다. 뒤쪽이 이
    랩이 다루는 실패이고, 검증 레이어가 잡으라고 만든 실패이기도 하다.
    """
    return [value for value in item.get("lure", []) if value in text]


def citations(text):
    """답변이 단 [line N] 인용 번호. step 0 에서 지어낸 인용을 세는 데 쓴다."""
    return re.findall(r"\[line\s*(\d+)\]", text)
