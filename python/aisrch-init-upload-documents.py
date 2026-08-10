#!/usr/bin/env python3

"""Build the lab's Azure AI Search indexes and load them. Run this on the jumpbox.

AI Search has public access and API keys both turned off, so the only place that can
reach it is inside the VNet. The jumpbox managed identity holds Search Service
Contributor (index definitions) and Search Index Data Contributor (documents).

Three indexes, one per source file under assets/tools/:

  housing    the KB housing market review markdown, split into chunks by heading
  merchants  KR_Merchants_Sample.csv, one document per merchant
  news       news_ko.csv, long articles split into chunks

Every index carries the same core field set so a Foundry agent can treat them
interchangeably as knowledge: a key, a title, a summary, the chunk text, a category,
and a vector of the chunk. Each also gets a semantic configuration, which is what
Foundry's Azure AI Search tool uses for semantic ranking.
"""

import argparse
import csv
import hashlib
import re
import sys
from pathlib import Path

from azure.core.exceptions import ResourceNotFoundError
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    HnswAlgorithmConfiguration,
    HnswParameters,
    SearchableField,
    SearchField,
    SearchFieldDataType,
    SearchIndex,
    SemanticConfiguration,
    SemanticField,
    SemanticPrioritizedFields,
    SemanticSearch,
    SimpleField,
    VectorSearch,
    VectorSearchAlgorithmMetric,
    VectorSearchProfile,
)
from openai import AzureOpenAI

import identity

# text-embedding-3-large 의 기본 차원 수. 배포 모델을 바꾸면 이 값도 바꿔야 한다.
EMBEDDING_DIMENSIONS = 3072

# 한 번의 임베딩 호출에 넣는 텍스트 개수. 너무 크면 요청이 토큰 한도에 걸린다.
EMBEDDING_BATCH_SIZE = 16

# 한 번의 업로드 요청에 넣는 문서 개수.
UPLOAD_BATCH_SIZE = 100

# 청크 크기. 한국어는 한 글자가 대략 한 토큰에 가까우므로 문자 수로 잘라도 편차가 작다.
CHUNK_CHARACTERS = 1200
CHUNK_OVERLAP_CHARACTERS = 150

# 요약이 따로 없는 원본에서 앞부분을 잘라 요약으로 쓸 때의 길이.
DERIVED_SUMMARY_CHARACTERS = 300

# 제목이 따로 없는 원본에서 앞부분을 잘라 제목으로 쓸 때의 길이.
DERIVED_TITLE_CHARACTERS = 60

# 한국어 본문에 맞는 분석기. 지정하지 않으면 공백 기준으로만 잘려 검색 품질이 떨어진다.
KOREAN_ANALYZER = "ko.microsoft"

VECTOR_PROFILE_NAME = "hnsw-profile"
VECTOR_ALGORITHM_NAME = "hnsw-config"
SEMANTIC_CONFIGURATION_NAME = "semantic-config"

# Search 문서 키에 쓸 수 있는 문자. 나머지는 전부 밑줄로 바꾼다.
KEY_SAFE_PATTERN = re.compile(r"[^A-Za-z0-9_\-=]")


# ---------------------------------------------------------------------------
# 인덱스 정의
# ---------------------------------------------------------------------------


def core_fields():
    """세 인덱스가 공통으로 갖는 필드.

    Foundry 가 지식 원본으로 붙일 때 기대하는 최소 구성이다. 제목과 본문은 검색 가능해야
    하고, 카테고리는 필터로 좁힐 수 있어야 하며, 벡터 필드가 있어야 하이브리드 검색이 된다.
    """
    return [
        SimpleField(
            name="id",
            type=SearchFieldDataType.String,
            key=True,
            filterable=True,
        ),
        SearchableField(
            name="title",
            type=SearchFieldDataType.String,
            analyzer_name=KOREAN_ANALYZER,
        ),
        SearchableField(
            name="summary",
            type=SearchFieldDataType.String,
            analyzer_name=KOREAN_ANALYZER,
        ),
        SearchableField(
            name="content",
            type=SearchFieldDataType.String,
            analyzer_name=KOREAN_ANALYZER,
        ),
        SearchableField(
            name="category",
            type=SearchFieldDataType.String,
            analyzer_name=KOREAN_ANALYZER,
            filterable=True,
            facetable=True,
        ),
        SimpleField(
            name="source",
            type=SearchFieldDataType.String,
            filterable=True,
            facetable=True,
        ),
        # 청크가 어느 원본 문서에서 나왔는지. 같은 문서의 다른 청크를 모아 볼 때 쓴다.
        SimpleField(
            name="parent_id",
            type=SearchFieldDataType.String,
            filterable=True,
        ),
        SimpleField(
            name="chunk_index",
            type=SearchFieldDataType.Int32,
            filterable=True,
            sortable=True,
        ),
        SearchField(
            name="content_vector",
            type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
            searchable=True,
            # 벡터 필드는 돌려받아도 쓸 데가 없고 응답만 키운다.
            retrievable=False,
            vector_search_dimensions=EMBEDDING_DIMENSIONS,
            vector_search_profile_name=VECTOR_PROFILE_NAME,
        ),
    ]


def merchant_fields():
    """merchants 인덱스에만 있는 필드.

    가맹점은 청크로 쪼갤 만큼 길지 않은 대신, 평점과 위치로 걸러 보는 것이 자연스럽다.
    그래서 숫자와 좌표를 문자열로 뭉뚱그리지 않고 타입을 살려 둔다.
    """
    return [
        SimpleField(
            name="rating",
            type=SearchFieldDataType.Double,
            filterable=True,
            sortable=True,
            facetable=True,
        ),
        SearchField(
            name="tags",
            type=SearchFieldDataType.Collection(SearchFieldDataType.String),
            searchable=True,
            filterable=True,
            facetable=True,
        ),
        SearchableField(
            name="street_address",
            type=SearchFieldDataType.String,
            analyzer_name=KOREAN_ANALYZER,
        ),
        SearchableField(
            name="city",
            type=SearchFieldDataType.String,
            analyzer_name=KOREAN_ANALYZER,
            filterable=True,
            facetable=True,
        ),
        SearchableField(
            name="state_province",
            type=SearchFieldDataType.String,
            analyzer_name=KOREAN_ANALYZER,
            filterable=True,
            facetable=True,
        ),
        SimpleField(
            name="postal_code",
            type=SearchFieldDataType.String,
            filterable=True,
        ),
        SimpleField(
            name="country",
            type=SearchFieldDataType.String,
            filterable=True,
            facetable=True,
        ),
        # 지리 좌표. 거리 기준 필터와 정렬에 쓴다.
        SimpleField(
            name="location",
            type=SearchFieldDataType.GeographyPoint,
            filterable=True,
            sortable=True,
        ),
    ]


def build_index(name, extra_fields=()):
    """인덱스 정의 하나를 만든다."""
    return SearchIndex(
        name=name,
        fields=list(core_fields()) + list(extra_fields),
        vector_search=VectorSearch(
            algorithms=[
                HnswAlgorithmConfiguration(
                    name=VECTOR_ALGORITHM_NAME,
                    parameters=HnswParameters(metric=VectorSearchAlgorithmMetric.COSINE),
                )
            ],
            profiles=[
                VectorSearchProfile(
                    name=VECTOR_PROFILE_NAME,
                    algorithm_configuration_name=VECTOR_ALGORITHM_NAME,
                )
            ],
        ),
        semantic_search=SemanticSearch(
            default_configuration_name=SEMANTIC_CONFIGURATION_NAME,
            configurations=[
                SemanticConfiguration(
                    name=SEMANTIC_CONFIGURATION_NAME,
                    prioritized_fields=SemanticPrioritizedFields(
                        title_field=SemanticField(field_name="title"),
                        content_fields=[
                            SemanticField(field_name="content"),
                            SemanticField(field_name="summary"),
                        ],
                        keywords_fields=[SemanticField(field_name="category")],
                    ),
                )
            ],
        ),
    )


# ---------------------------------------------------------------------------
# 텍스트 다듬기
# ---------------------------------------------------------------------------


def make_key(*parts):
    """Search 문서 키로 쓸 수 있는 문자열을 만든다."""
    raw = "-".join(str(part) for part in parts)
    safe = KEY_SAFE_PATTERN.sub("_", raw)
    # 한국어가 통째로 밑줄이 되면 서로 구분되지 않으므로 해시를 덧붙인다.
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    return f"{safe[:96]}-{digest}"


def shorten(text, limit):
    """문장 경계를 존중해서 앞부분만 남긴다."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    cut = collapsed[:limit]
    # 마지막 마침표까지만 남긴다. 없으면 그냥 자른다.
    boundary = max(cut.rfind("."), cut.rfind("다 "), cut.rfind("? "), cut.rfind("! "))
    if boundary > limit // 2:
        return cut[: boundary + 1].strip()
    return cut.strip()


def chunk_text(text):
    """긴 본문을 겹침을 두고 자른다.

    문단 경계를 먼저 찾고, 한 문단이 통째로 한도를 넘으면 그때만 글자 수로 자른다.
    표나 목록이 중간에서 끊기는 것을 줄이기 위해서다.
    """
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks = []
    buffer = ""

    for paragraph in paragraphs:
        if len(paragraph) > CHUNK_CHARACTERS:
            if buffer:
                chunks.append(buffer)
                buffer = ""
            start = 0
            while start < len(paragraph):
                chunks.append(paragraph[start : start + CHUNK_CHARACTERS])
                start += CHUNK_CHARACTERS - CHUNK_OVERLAP_CHARACTERS
            continue

        candidate = f"{buffer}\n\n{paragraph}" if buffer else paragraph
        if len(candidate) > CHUNK_CHARACTERS:
            chunks.append(buffer)
            buffer = paragraph
        else:
            buffer = candidate

    if buffer:
        chunks.append(buffer)
    return chunks


def split_markdown_sections(markdown):
    """마크다운을 제목 단위로 나눈다. (제목 경로, 본문) 목록을 돌려준다."""
    sections = []
    heading_stack = []
    current_lines = []

    def flush():
        if not current_lines:
            return
        body = "\n".join(current_lines).strip()
        if body:
            path = " > ".join(heading_stack) if heading_stack else "문서 시작"
            sections.append((path, body))

    for line in markdown.splitlines():
        match = re.match(r"^(#{1,6})\s+(.*)$", line)
        if match:
            flush()
            current_lines = []
            level = len(match.group(1))
            title = match.group(2).strip()
            # 같은 깊이 이하의 제목은 걷어내고 이번 제목을 쌓는다.
            heading_stack[level - 1 :] = [title]
            continue
        current_lines.append(line)

    flush()
    return sections


# ---------------------------------------------------------------------------
# 원본별 문서 만들기
# ---------------------------------------------------------------------------


def build_housing_documents(path):
    """KB 주택시장리뷰 마크다운 → housing 문서."""
    markdown = path.read_text(encoding="utf-8")
    documents = []

    for section_index, (heading_path, body) in enumerate(split_markdown_sections(markdown)):
        title = heading_path.split(" > ")[-1]
        for chunk_index, chunk in enumerate(chunk_text(body)):
            documents.append(
                {
                    "id": make_key("housing", section_index, chunk_index),
                    "title": title,
                    # 제목 경로를 요약 앞에 붙여, 청크만 봐도 문서 어디쯤인지 알 수 있게 한다.
                    "summary": f"{heading_path} — {shorten(chunk, DERIVED_SUMMARY_CHARACTERS)}",
                    "content": chunk,
                    "category": "주택시장",
                    "source": path.name,
                    "parent_id": make_key("housing", section_index),
                    "chunk_index": chunk_index,
                }
            )
    return documents


def build_merchant_documents(path):
    """가맹점 CSV → merchants 문서. 한 행이 한 문서다."""
    documents = []

    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            name = (row.get("MerchantName") or "").strip()
            description = (row.get("Description") or "").strip()
            tags = [t.strip() for t in (row.get("Tag") or "").split(",") if t.strip()]
            address = " ".join(
                part
                for part in (
                    (row.get("StreetAddress") or "").strip(),
                    (row.get("City") or "").strip(),
                )
                if part
            )

            # 임베딩과 키워드 검색이 함께 볼 본문. 흩어진 열을 한 문장으로 모은다.
            content = " / ".join(
                part
                for part in (
                    name,
                    (row.get("Category") or "").strip(),
                    description,
                    f"태그: {', '.join(tags)}" if tags else "",
                    f"주소: {address}" if address else "",
                    f"평점: {row.get('Rating')}" if row.get("Rating") else "",
                )
                if part
            )

            document = {
                "id": make_key("merchant", row.get("MerchantId") or name),
                "title": name,
                "summary": description or name,
                "content": content,
                "category": (row.get("Category") or "").strip(),
                "source": path.name,
                "parent_id": (row.get("MerchantId") or "").strip(),
                "chunk_index": 0,
                "tags": tags,
                "street_address": (row.get("StreetAddress") or "").strip(),
                "city": (row.get("City") or "").strip(),
                "state_province": (row.get("StateProvince") or "").strip(),
                "postal_code": (row.get("PostalCode") or "").strip(),
                "country": (row.get("Country") or "").strip(),
            }

            rating = (row.get("Rating") or "").strip()
            if rating:
                document["rating"] = float(rating)

            longitude = (row.get("Longitude") or "").strip()
            latitude = (row.get("Latitude") or "").strip()
            if longitude and latitude:
                # Search 의 GeographyPoint 는 GeoJSON 과 같은 (경도, 위도) 순서다.
                document["location"] = {
                    "type": "Point",
                    "coordinates": [float(longitude), float(latitude)],
                }

            documents.append(document)
    return documents


def build_news_documents(path):
    """뉴스/문서 CSV → news 문서. 본문이 길어 청크로 나눈다."""
    documents = []

    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            article_id = (row.get("id") or "").strip()
            summary = (row.get("summary") or "").strip()
            content = (row.get("content") or "").strip()
            if not content:
                continue

            # 이 CSV 에는 제목 열이 없다. 요약 앞머리를 제목으로 쓴다.
            title = shorten(summary or content, DERIVED_TITLE_CHARACTERS)

            for chunk_index, chunk in enumerate(chunk_text(content)):
                documents.append(
                    {
                        "id": make_key("news", article_id, chunk_index),
                        "title": title,
                        "summary": summary or shorten(chunk, DERIVED_SUMMARY_CHARACTERS),
                        "content": chunk,
                        "category": (row.get("category") or "").strip(),
                        "source": path.name,
                        "parent_id": article_id,
                        "chunk_index": chunk_index,
                    }
                )
    return documents


# 인덱스 이름 -> (원본 파일명, 문서 생성 함수, 추가 필드)
SOURCES = {
    "housing": ("KB주택시장리뷰_2025년 10월호.md", build_housing_documents, ()),
    "merchants": ("KR_Merchants_Sample.csv", build_merchant_documents, merchant_fields()),
    "news": ("news_ko.csv", build_news_documents, ()),
}


# ---------------------------------------------------------------------------
# 임베딩과 업로드
# ---------------------------------------------------------------------------


def embed_documents(client, deployment, documents):
    """문서의 content 를 임베딩해 content_vector 에 채운다."""
    for start in range(0, len(documents), EMBEDDING_BATCH_SIZE):
        batch = documents[start : start + EMBEDDING_BATCH_SIZE]
        response = client.embeddings.create(
            model=deployment,
            input=[document["content"] for document in batch],
        )
        for document, item in zip(batch, response.data):
            document["content_vector"] = item.embedding
        print(f"    임베딩 {min(start + len(batch), len(documents))}/{len(documents)}")


def upload_documents(client, documents):
    """업로드하고 실패한 문서가 있으면 알린다."""
    failures = []
    for start in range(0, len(documents), UPLOAD_BATCH_SIZE):
        batch = documents[start : start + UPLOAD_BATCH_SIZE]
        for result in client.upload_documents(documents=batch):
            if not result.succeeded:
                failures.append(f"{result.key}: {result.error_message}")
        print(f"    업로드 {min(start + len(batch), len(documents))}/{len(documents)}")

    if failures:
        raise RuntimeError(
            "업로드에 실패한 문서가 있습니다.\n  " + "\n  ".join(failures[:10])
        )


def ensure_index(index_client, index, recreate):
    """인덱스를 만들거나 갱신한다."""
    try:
        index_client.get_index(index.name)
        exists = True
    except ResourceNotFoundError:
        exists = False

    if exists and recreate:
        print(f"    기존 인덱스 삭제: {index.name}")
        index_client.delete_index(index.name)
        exists = False

    if exists:
        # 필드를 지우거나 타입을 바꾸는 변경은 거절된다. 그때는 --recreate 를 쓴다.
        index_client.create_or_update_index(index)
        print(f"    인덱스 갱신: {index.name}")
    else:
        index_client.create_index(index)
        print(f"    인덱스 생성: {index.name}")


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------


def parse_args():
    parser = argparse.ArgumentParser(
        description="실습용 Azure AI Search 인덱스를 만들고 문서를 넣는다. 점프박스에서 실행한다.",
        epilog="AI Search 와 Foundry 둘 다 Private Endpoint 로만 열려 있다. VNet 밖에서 "
               "실행하면 이름 해석 단계에서 실패한다. 인증은 관리 ID 를 쓰므로 점프박스에서는 "
               "--auth managed-identity 가 가장 확실하다.",
    )
    parser.add_argument("--search-endpoint", required=True,
                        help="AI Search 엔드포인트, https://<서비스명>.search.windows.net")
    parser.add_argument("--foundry-endpoint", required=True,
                        help="임베딩을 호출할 Foundry 계정 엔드포인트")
    parser.add_argument("--deployment", default="text-embedding-3-large",
                        help="임베딩 모델 배포 이름")
    parser.add_argument("--assets", default=str(Path(__file__).parent / "assets" / "tools"),
                        help="원본 파일이 있는 디렉터리")
    parser.add_argument("--index", action="append", choices=sorted(SOURCES),
                        help="만들 인덱스. 반복 지정할 수 있고, 생략하면 전부 만든다.")
    parser.add_argument("--prefix", default="",
                        help="인덱스 이름 앞에 붙일 접두사. 한 서비스에 여러 실습을 둘 때 쓴다.")
    parser.add_argument("--recreate", action="store_true",
                        help="기존 인덱스를 지우고 다시 만든다. 필드 구성을 바꿨을 때 필요하다.")
    parser.add_argument("--api-version", default="2024-10-21",
                        help="임베딩 호출에 쓸 Azure OpenAI API 버전")

    identity.add_auth_arguments(parser)
    return parser.parse_args()


def main():
    args = parse_args()
    assets = Path(args.assets)
    if not assets.is_dir():
        sys.exit(f"원본 디렉터리를 찾지 못했습니다: {assets}")

    targets = args.index or sorted(SOURCES)

    # 없는 파일은 먼저 걸러 낸다. 인덱스를 만들어 놓고 중간에 멈추는 것보다 낫다.
    missing = [
        SOURCES[name][0] for name in targets if not (assets / SOURCES[name][0]).is_file()
    ]
    if missing:
        sys.exit("원본 파일이 없습니다:\n  " + "\n  ".join(missing))

    credential = identity.get_credential(args)
    if credential is None:
        sys.exit("이 스크립트는 Entra ID 인증만 지원합니다. --auth 를 키가 아닌 값으로 지정하세요.")

    # 임베딩은 데이터 평면 호출이라 고전 스코프를 쓴다.
    from azure.identity import get_bearer_token_provider

    token_provider = get_bearer_token_provider(
        credential, "https://cognitiveservices.azure.com/.default"
    )
    openai_client = AzureOpenAI(
        azure_endpoint=args.foundry_endpoint,
        azure_ad_token_provider=token_provider,
        api_version=args.api_version,
    )

    index_client = SearchIndexClient(endpoint=args.search_endpoint, credential=credential)

    for name in targets:
        filename, builder, extra_fields = SOURCES[name]
        index_name = f"{args.prefix}{name}"
        print(f"==> {index_name} ({filename})")

        documents = builder(assets / filename)
        if not documents:
            print("    문서가 없습니다. 건너뜁니다.")
            continue
        print(f"    문서 {len(documents)}건")

        ensure_index(index_client, build_index(index_name, extra_fields), args.recreate)
        embed_documents(openai_client, args.deployment, documents)

        search_client = SearchClient(
            endpoint=args.search_endpoint, index_name=index_name, credential=credential
        )
        upload_documents(search_client, documents)
        print(f"    완료: {index_name}")

    print()
    print("Foundry 에 지식으로 붙일 때는 프로젝트의 AI Search 연결을 고르고 위 인덱스 이름을")
    print("지정하면 됩니다. 의미 체계 구성 이름은 " + SEMANTIC_CONFIGURATION_NAME + " 입니다.")


if __name__ == "__main__":
    main()
