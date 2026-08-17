"""The question set every step is measured against, and the document it comes from.

Shared so that step 0 and step 5 are answering the identical questions. If each step
carried its own examples, a number moving between steps could always be the questions
having changed, and nothing in the lab would be evidence of anything.
"""

import os

PYTHON_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DOCUMENT = os.path.join(PYTHON_DIR, "assets", "tools", "KB주택시장리뷰_2025년 10월호.md")

# Questions whose answers are single figures printed in the report, so a wrong answer is
# wrong on its face and no judge is needed to see it. source_lines says where the answer
# lives; the text of those lines is sliced out of the file at run time, never copied here.
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
        # The Seoul figure is not in the prose — it lives in a chart's alt text, a bullet
        # under it, and the chart's JSON. Skimming the narrative misses it; searching
        # finds it. That gap is why this question is in the set.
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
    """Read the file once and hand back both forms it gets used in.

    Steps read this many times per question. Reading per call would put disk time into
    the seconds column, which is meant to measure the harness and nothing else.
    """
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    return text, text.splitlines()


def resolve_golden(lines, count=None):
    """Slice each question's supporting text out of the document instead of storing it.

    Copying those paragraphs in here would read better and be quietly wrong: the moment
    the document is re-extracted the copy drifts, and from then on step 5 scores answers
    against sentences that are not in the file the agent is searching. Every step would
    keep printing numbers and every number would be a lie.
    """
    resolved = []
    for item in GOLDEN[:count]:
        excerpt = []
        for first, last in item["source_lines"]:
            excerpt += [f"{n}: {lines[n - 1]}" for n in range(first, last + 1) if n <= len(lines)]
        resolved.append({**item, "context": "\n".join(excerpt)})
    return resolved


def is_hit(item, text):
    """Every key must appear literally. No model involved, so this number costs nothing
    and reads the same on every run — which is why it leads every report."""
    return all(key in text for key in item["answer_key"])
