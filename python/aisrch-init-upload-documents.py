#!/usr/bin/env python3

"""실습용 Azure AI Search 인덱스를 만들고 문서를 넣는다. 점프박스에서 실행한다.

AI Search 는 공용 접근과 API 키가 둘 다 꺼져 있어, 닿을 수 있는 곳은 VNet 안뿐이다.
점프박스 관리 ID 가 Search Service Contributor(인덱스 정의)와 Search Index Data
Contributor(문서)를 갖고 있다.

원본 파일마다 같은 이름의 JSON 을 옆에 둔다. 그 JSON 이 인덱스 하나를 기술한다.

    assets/tools/news_ko.csv     원본
    assets/tools/news_ko.json    그 원본을 어떻게 인덱스로 만들지

원본이 하나 늘면 파일을 놓고 JSON 을 하나 쓰면 된다. 이 스크립트는 고치지 않는다.

    python aisrch-init-upload-documents.py \\
        --search-endpoint https://<서비스명>.search.windows.net \\
        --foundry-endpoint https://<리소스>.cognitiveservices.azure.com \\
        --auth managed-identity

스키마 한 벌은 이렇게 생겼다. 원본 경로와 형식은 짝이 되는 파일에서 나오므로 적지 않는다.

    {
      "index": "news",
      "analyzer": "ko.microsoft",
      "require": ["content"],
      "chunk": {"from": "content", "size": 1200, "overlap": 150},
      "vector": {"name": "content_vector", "from": "content", "dimensions": 3072},
      "semantic": {"title": "title", "content": ["content"], "keywords": ["category"]},
      "fields": [...]
    }

원본은 레코드의 열(列)로 읽힌다. CSV 는 열 이름이 그대로 쓰이고, 마크다운은
heading / heading_path / body 를 갖는다. 어느 형식이든 다음 값이 함께 붙는다:

    _source       원본 파일 이름
    _index        원본에서 몇 번째 레코드인지
    _chunk        이 문서가 담당하는 본문 조각 (chunk 를 쓰지 않으면 빈 문자열)
    _chunk_index  그 조각이 레코드 안에서 몇 번째인지

필드 값은 from / key_from / split / point 중 하나로 정한다. 자세한 형태는
resolve_value 와 part_text 의 주석에 있다.
"""

import argparse
import csv
import hashlib
import json
import re
import sys
from pathlib import Path

from azure.core.exceptions import ResourceNotFoundError
from azure.identity import get_bearer_token_provider
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

# 청크 크기. 한국어는 한 글자가 대략 한 토큰에 가까우므로 문자 수로 잘라도 편차가 작다.
CHUNK_CHARACTERS = 1200
CHUNK_OVERLAP_CHARACTERS = 150

# 한국어 본문에 맞는 기본 분석기. 지정하지 않으면 공백 기준으로만 잘려 검색 품질이 떨어진다.
KOREAN_ANALYZER = "ko.microsoft"

VECTOR_PROFILE_NAME = "hnsw-profile"
VECTOR_ALGORITHM_NAME = "hnsw-config"
SEMANTIC_CONFIGURATION_NAME = "semantic-config"

# 한 번의 임베딩 호출에 넣는 텍스트 개수. 너무 크면 요청이 토큰 한도에 걸린다.
EMBEDDING_BATCH_SIZE = 16

# 한 번의 업로드 요청에 넣는 문서 개수.
UPLOAD_BATCH_SIZE = 100

# 실패한 문서를 보고할 때 몇 개까지 보여 줄지.
MAX_REPORTED_FAILURES = 10

# 임베딩은 데이터 평면 호출이라 고전 스코프를 쓴다.
EMBEDDING_SCOPE = "https://cognitiveservices.azure.com/.default"

# Search 문서 키에 쓸 수 있는 문자. 나머지는 전부 밑줄로 바꾼다.
KEY_SAFE_PATTERN = re.compile(r"[^A-Za-z0-9_\-=]")

FIELD_TYPES = {
    "string": SearchFieldDataType.String,
    "int": SearchFieldDataType.Int32,
    "double": SearchFieldDataType.Double,
    "bool": SearchFieldDataType.Boolean,
    "collection": SearchFieldDataType.Collection(SearchFieldDataType.String),
    "geo": SearchFieldDataType.GeographyPoint,
}

TRUE_WORDS = ("1", "true", "yes", "y", "예")

# 값의 출처. 필드마다 정확히 하나만 있어야 한다.
VALUE_KEYS = ("from", "key_from", "split", "point")

SCHEMA_KEYS = {"index", "format", "analyzer", "require", "chunk", "vector", "semantic", "fields"}
FIELD_KEYS = {"name", "type", "key", "searchable", "filterable", "facetable", "sortable",
              "analyzer", "separator", "shorten", *VALUE_KEYS}
PART_KEYS = {"column", "columns", "const", "label", "separator", "shorten", "first"}

DEFAULT_SCHEMA_DIR = Path(__file__).parent / "assets" / "tools"


class SchemaError(Exception):
    """스키마가 잘못되었을 때. 어느 파일인지는 메시지에 함께 담는다."""


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


def chunk_text(text, size=CHUNK_CHARACTERS, overlap=CHUNK_OVERLAP_CHARACTERS):
    """긴 본문을 겹침을 두고 자른다.

    문단 경계를 먼저 찾고, 한 문단이 통째로 한도를 넘으면 그때만 글자 수로 자른다.
    표나 목록이 중간에서 끊기는 것을 줄이기 위해서다.
    """
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks = []
    buffer = ""

    for paragraph in paragraphs:
        if len(paragraph) > size:
            if buffer:
                chunks.append(buffer)
                buffer = ""
            start = 0
            while start < len(paragraph):
                chunks.append(paragraph[start : start + size])
                start += size - overlap
            continue

        candidate = f"{buffer}\n\n{paragraph}" if buffer else paragraph
        if len(candidate) > size:
            chunks.append(buffer)
            buffer = paragraph
        else:
            buffer = candidate

    if buffer:
        chunks.append(buffer)
    return chunks


# ---------------------------------------------------------------------------
# 원본 읽기
# ---------------------------------------------------------------------------


def read_csv_records(path):
    """CSV 한 행이 레코드 하나. 열 이름이 그대로 레코드의 키가 된다.

    앞뒤 공백은 여기서 한 번만 걷어낸다. 값을 꺼낼 때마다 걷어내면 본문을 잘라
    만든 조각의 경계에 있던 공백까지 사라진다.
    """
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [{key: (value or "").strip() for key, value in row.items()}
                for row in csv.DictReader(handle)]


def read_markdown_records(path):
    """마크다운을 제목 단위로 나눈다. 제목 하나가 레코드 하나다."""
    records = []
    heading_stack = []
    current_lines = []

    def flush():
        if not current_lines:
            return
        body = "\n".join(current_lines).strip()
        if not body:
            return
        records.append({
            "heading": heading_stack[-1] if heading_stack else "문서 시작",
            "heading_path": " > ".join(heading_stack) if heading_stack else "문서 시작",
            "body": body,
        })

    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^(#{1,6})\s+(.*)$", line)
        if match:
            flush()
            current_lines = []
            # 같은 깊이 이하의 제목은 걷어내고 이번 제목을 쌓는다.
            heading_stack[len(match.group(1)) - 1 :] = [match.group(2).strip()]
            continue
        current_lines.append(line)

    flush()
    return records


READERS = {"csv": read_csv_records, "markdown": read_markdown_records}

# 확장자로 형식을 정한다. 여기 없는 확장자는 스키마의 format 으로 알려 줘야 한다.
EXTENSION_FORMATS = {".csv": "csv", ".md": "markdown", ".markdown": "markdown"}


def find_source(schema_path):
    """스키마 옆에 같은 이름으로 놓인 원본 파일을 찾는다."""
    matches = sorted(entry for entry in schema_path.parent.iterdir()
                     if entry.is_file() and entry != schema_path
                     and entry.stem == schema_path.stem)
    if not matches:
        raise SystemExit(f"{schema_path.name} 과 짝이 되는 원본 파일이 "
                         f"{schema_path.parent} 에 없습니다")
    if len(matches) > 1:
        names = ", ".join(entry.name for entry in matches)
        raise SystemExit(f"{schema_path.name} 과 짝이 될 파일이 여럿입니다: {names}")
    return matches[0]


def resolve_format(schema, source, where):
    kind = schema.get("format") or EXTENSION_FORMATS.get(source.suffix.lower())
    if not kind:
        raise SchemaError(f"{where}: {source.suffix} 는 아는 확장자가 아닙니다. "
                          f"스키마에 format 을 적으세요. 쓸 수 있는 값: {', '.join(sorted(READERS))}")
    return kind


# ---------------------------------------------------------------------------
# 값 만들기
# ---------------------------------------------------------------------------


def record_text(record, column):
    """레코드에서 한 값을 문자열로 꺼낸다. 없거나 None 이면 빈 문자열.

    공백은 원본을 읽을 때 이미 정리했으므로 여기서 손대지 않는다. _chunk 는
    본문에서 잘라 낸 그대로여야 한다.
    """
    value = record.get(column)
    return "" if value is None else str(value)


def part_text(part, record):
    """조각 하나를 문자열로 만든다.

    조각은 열 이름이거나 다음 중 하나를 담은 객체다:

      {"const": "housing"}                        고정 문자열
      {"column": "Tag", "label": "태그"}            열 하나, 라벨을 앞에 붙임
      {"columns": ["StreetAddress", "City"]}      여러 열을 separator 로 이음
      {"first": ["summary", "content"]}           먼저 값이 있는 쪽
      {"column": "_chunk", "shorten": 300}        앞부분만

    값이 비어 있으면 빈 문자열을 돌려주고, 이어 붙일 때 그 조각은 빠진다.
    """
    if isinstance(part, str):
        return record_text(record, part)

    if "const" in part:
        return str(part["const"])

    if "first" in part:
        text = next((t for t in (part_text(p, record) for p in part["first"]) if t), "")
    else:
        columns = part.get("columns") or [part["column"]]
        separator = part.get("separator", " ")
        text = separator.join(t for t in (record_text(record, c) for c in columns) if t)

    if not text:
        return ""
    if part.get("shorten"):
        text = shorten(text, part["shorten"])
    return f"{part['label']}: {text}" if part.get("label") else text


def as_parts(value):
    return value if isinstance(value, list) else [value]


def resolve_point(spec, record):
    """좌표. 경도와 위도가 둘 다 있어야 값이 된다."""
    longitude = record_text(record, spec["longitude"])
    latitude = record_text(record, spec["latitude"])
    if not (longitude and latitude):
        return None
    # Search 의 GeographyPoint 는 GeoJSON 과 같은 (경도, 위도) 순서다.
    return {"type": "Point", "coordinates": [float(longitude), float(latitude)]}


def resolve_split(spec, record):
    """한 열에 몰아 넣은 목록을 컬렉션으로 편다."""
    text = record_text(record, spec["column"])
    return [item.strip() for item in text.split(spec.get("separator", ",")) if item.strip()]


def resolve_value(spec, record):
    """필드 하나의 값. from / key_from / split / point 중 하나로 정해진다."""
    if "point" in spec:
        return resolve_point(spec["point"], record)
    if "split" in spec:
        return resolve_split(spec["split"], record)
    if "key_from" in spec:
        return make_key(*(part_text(part, record) for part in as_parts(spec["key_from"])))

    separator = spec.get("separator", " ")
    text = separator.join(t for t in (part_text(p, record) for p in as_parts(spec["from"])) if t)
    return shorten(text, spec["shorten"]) if (text and spec.get("shorten")) else text


def convert(value, spec):
    """선언한 타입으로 바꾼다. 값이 비어 있으면 None — 그런 필드는 문서에서 빠진다."""
    if value is None or value == "" or value == []:
        return None

    kind = spec.get("type", "string")
    try:
        if kind == "int":
            return int(float(value))
        if kind == "double":
            return float(value)
        if kind == "bool":
            return str(value).strip().lower() in TRUE_WORDS
    except (TypeError, ValueError):
        raise SchemaError(f"{spec['name']} 필드에 {kind} 로 바꿀 수 없는 값이 있습니다: {value!r}") from None
    return value


# ---------------------------------------------------------------------------
# 문서 만들기
# ---------------------------------------------------------------------------


def expand_chunks(record, chunk):
    """레코드 하나를 문서 하나 또는 여럿으로 편다."""
    if not chunk:
        return [{**record, "_chunk": "", "_chunk_index": 0}]

    pieces = chunk_text(record_text(record, chunk["from"]),
                        chunk.get("size", CHUNK_CHARACTERS),
                        chunk.get("overlap", CHUNK_OVERLAP_CHARACTERS))
    return [{**record, "_chunk": piece, "_chunk_index": index}
            for index, piece in enumerate(pieces)]


def build_document(schema, record):
    document = {}
    for spec in schema["fields"]:
        value = convert(resolve_value(spec, record), spec)
        if value is not None:
            document[spec["name"]] = value
    return document


def build_documents(schema, source):
    """원본 파일 하나를 인덱스에 올릴 문서 목록으로 바꾼다."""
    required = schema.get("require", [])
    documents = []

    for index, record in enumerate(READERS[schema["format"]](source)):
        if any(not record_text(record, column) for column in required):
            continue
        record = {**record, "_source": source.name, "_index": index}
        documents.extend(build_document(schema, expanded)
                         for expanded in expand_chunks(record, schema.get("chunk")))
    return documents


# ---------------------------------------------------------------------------
# 인덱스 정의 만들기
# ---------------------------------------------------------------------------


def build_field(spec, default_analyzer):
    attributes = {
        "name": spec["name"],
        "filterable": spec.get("filterable", False),
        "facetable": spec.get("facetable", False),
        "sortable": spec.get("sortable", False),
    }
    kind = spec.get("type", "string")

    if spec.get("key"):
        # 키는 검색 대상이 아니라 식별자다.
        return SimpleField(**attributes, type=FIELD_TYPES[kind], key=True)
    if kind == "collection":
        return SearchField(**attributes, type=FIELD_TYPES[kind],
                           searchable=spec.get("searchable", False))
    if kind == "string" and spec.get("searchable"):
        return SearchableField(**attributes, type=FIELD_TYPES[kind],
                               analyzer_name=spec.get("analyzer", default_analyzer))
    return SimpleField(**attributes, type=FIELD_TYPES[kind])


def build_vector_field(vector):
    return SearchField(
        name=vector["name"],
        type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
        searchable=True,
        # 벡터 필드는 돌려받아도 쓸 데가 없고 응답만 키운다.
        retrievable=False,
        vector_search_dimensions=vector["dimensions"],
        vector_search_profile_name=VECTOR_PROFILE_NAME,
    )


def build_semantic_search(semantic):
    """의미 체계 구성. Foundry 의 AI Search 도구가 의미 순위에 쓴다."""
    if not semantic:
        return None
    return SemanticSearch(
        default_configuration_name=SEMANTIC_CONFIGURATION_NAME,
        configurations=[
            SemanticConfiguration(
                name=SEMANTIC_CONFIGURATION_NAME,
                prioritized_fields=SemanticPrioritizedFields(
                    title_field=SemanticField(field_name=semantic["title"]),
                    content_fields=[SemanticField(field_name=name)
                                    for name in semantic.get("content", [])],
                    keywords_fields=[SemanticField(field_name=name)
                                     for name in semantic.get("keywords", [])],
                ),
            )
        ],
    )


def build_index(schema, index_name):
    """스키마 한 벌을 인덱스 정의 하나로 바꾼다."""
    analyzer = schema.get("analyzer", KOREAN_ANALYZER)
    return SearchIndex(
        name=index_name,
        fields=[build_field(spec, analyzer) for spec in schema["fields"]]
               + [build_vector_field(schema["vector"])],
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
        semantic_search=build_semantic_search(schema.get("semantic")),
    )


# ---------------------------------------------------------------------------
# 스키마 읽기와 검사
# ---------------------------------------------------------------------------


def check_unknown(keys, allowed, where):
    unknown = sorted(set(keys) - allowed)
    if unknown:
        raise SchemaError(f"{where} 에 모르는 키가 있습니다: {', '.join(unknown)}")


def validate_part(part, where):
    """조각 하나를 검사한다. 오타를 서비스가 아니라 여기서 잡기 위한 것이다."""
    if isinstance(part, str):
        return
    if not isinstance(part, dict):
        raise SchemaError(f"{where} 의 조각은 열 이름이거나 객체여야 합니다: {part!r}")

    check_unknown(part, PART_KEYS, where)
    sources = [key for key in ("const", "column", "columns", "first") if key in part]
    if len(sources) != 1:
        raise SchemaError(f"{where} 의 조각은 const / column / columns / first 중 "
                          f"하나만 가져야 합니다: {part!r}")
    for nested in part.get("first", []):
        validate_part(nested, where)


def validate_field(spec, where):
    if not isinstance(spec, dict) or not spec.get("name"):
        raise SchemaError(f"{where} 의 필드에 name 이 없습니다: {spec!r}")

    where = f"{where} 의 {spec['name']} 필드"
    check_unknown(spec, FIELD_KEYS, where)

    kind = spec.get("type", "string")
    if kind not in FIELD_TYPES:
        raise SchemaError(f"{where} 의 type 이 {kind} 입니다. "
                          f"쓸 수 있는 값: {', '.join(sorted(FIELD_TYPES))}")

    sources = [key for key in VALUE_KEYS if key in spec]
    if len(sources) != 1:
        raise SchemaError(f"{where} 는 {' / '.join(VALUE_KEYS)} 중 하나만 가져야 합니다")

    for key in ("from", "key_from"):
        for part in as_parts(spec.get(key, [])):
            validate_part(part, where)
    if "point" in spec and not (spec["point"].get("longitude") and spec["point"].get("latitude")):
        raise SchemaError(f"{where} 의 point 에는 longitude 와 latitude 가 모두 있어야 합니다")
    if "split" in spec and not spec["split"].get("column"):
        raise SchemaError(f"{where} 의 split 에는 column 이 있어야 합니다")


def validate_references(schema, names, where):
    """vector 와 semantic 이 실제로 있는 필드를 가리키는지 본다."""
    vector = schema["vector"]
    for key in ("name", "from", "dimensions"):
        if not vector.get(key):
            raise SchemaError(f"{where} 의 vector 에 {key} 가 없습니다")
    if vector["from"] not in names:
        raise SchemaError(f"{where} 의 vector.from 이 없는 필드를 가리킵니다: {vector['from']}")
    if vector["name"] in names:
        raise SchemaError(f"{where} 의 vector.name 이 필드 이름과 겹칩니다: {vector['name']}")

    semantic = schema.get("semantic")
    if not semantic:
        return
    if not semantic.get("title"):
        raise SchemaError(f"{where} 의 semantic 에 title 이 없습니다")
    referenced = [semantic["title"], *semantic.get("content", []), *semantic.get("keywords", [])]
    missing = sorted(set(referenced) - names)
    if missing:
        raise SchemaError(f"{where} 의 semantic 이 없는 필드를 가리킵니다: {', '.join(missing)}")


def validate_schema(schema, where):
    if not isinstance(schema, dict):
        raise SchemaError(f"{where} 는 객체 하나여야 합니다")
    check_unknown(schema, SCHEMA_KEYS, where)

    for key in ("index", "fields", "vector"):
        if not schema.get(key):
            raise SchemaError(f"{where} 에 {key} 가 없습니다")
    if schema.get("format") and schema["format"] not in READERS:
        raise SchemaError(f"{where} 의 format 이 {schema['format']} 입니다. "
                          f"쓸 수 있는 값: {', '.join(sorted(READERS))}")
    if schema.get("chunk") and not schema["chunk"].get("from"):
        raise SchemaError(f"{where} 의 chunk 에는 from 이 있어야 합니다")

    for spec in schema["fields"]:
        validate_field(spec, where)

    names = [spec["name"] for spec in schema["fields"]]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise SchemaError(f"{where} 에 같은 이름의 필드가 있습니다: {', '.join(duplicates)}")

    keys = [spec["name"] for spec in schema["fields"] if spec.get("key")]
    if len(keys) != 1:
        raise SchemaError(f"{where} 에는 key 필드가 정확히 하나 있어야 합니다. 지금은 {len(keys)} 개입니다")

    validate_references(schema, set(names), where)


def load_schema(path):
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise SchemaError(f"{path} 를 읽지 못했습니다: {error}") from None

    validate_schema(schema, str(path))
    return schema


# ---------------------------------------------------------------------------
# 인덱스에 올리기
# ---------------------------------------------------------------------------


def embed_documents(client, deployment, documents, vector):
    """문서의 원본 텍스트를 임베딩해 벡터 필드에 채운다."""
    for start in range(0, len(documents), EMBEDDING_BATCH_SIZE):
        batch = documents[start : start + EMBEDDING_BATCH_SIZE]
        response = client.embeddings.create(
            model=deployment,
            input=[document.get(vector["from"], "") for document in batch],
        )
        for document, item in zip(batch, response.data):
            document[vector["name"]] = item.embedding
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
            "업로드에 실패한 문서가 있습니다.\n  " + "\n  ".join(failures[:MAX_REPORTED_FAILURES])
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
        description="원본 파일 옆의 같은 이름 JSON 을 읽어 Azure AI Search 인덱스를 만들고 "
                    "문서를 넣는다. 점프박스에서 실행한다.",
        epilog="AI Search 와 Foundry 둘 다 Private Endpoint 로만 열려 있다. VNet 밖에서 "
               "실행하면 이름 해석 단계에서 실패한다. 인증은 관리 ID 를 쓰므로 점프박스에서는 "
               "--auth managed-identity 가 가장 확실하다. --schema 를 생략하면 "
               f"{DEFAULT_SCHEMA_DIR.name} 안의 *.json 을 전부 올린다.",
    )
    parser.add_argument("--search-endpoint", required=True,
                        help="AI Search 엔드포인트, https://<서비스명>.search.windows.net")
    parser.add_argument("--foundry-endpoint", required=True,
                        help="임베딩을 호출할 Foundry 계정 엔드포인트")
    parser.add_argument("--deployment", default="text-embedding-3-large",
                        help="임베딩 모델 배포 이름")

    parser.add_argument("--schema", action="append", default=[], metavar="JSON", type=Path,
                        help="인덱스 정의 파일. 원본은 같은 이름의 다른 확장자로 찾는다. 반복 지정할 수 있다.")
    parser.add_argument("--index-name", metavar="NAME",
                        help="스키마의 index 를 이 이름으로 덮어쓴다. --schema 가 하나일 때만 쓸 수 있다.")
    parser.add_argument("--prefix", default="",
                        help="인덱스 이름 앞에 붙일 접두사. 한 서비스에 여러 실습을 둘 때 쓴다.")
    parser.add_argument("--recreate", action="store_true",
                        help="기존 인덱스를 지우고 다시 만든다. 필드 구성을 바꿨을 때 필요하다.")
    parser.add_argument("--dry-run", action="store_true",
                        help="문서만 만들어 보고 인덱스도 임베딩도 건드리지 않는다.")
    parser.add_argument("--api-version", default="2024-10-21",
                        help="임베딩 호출에 쓸 Azure OpenAI API 버전")

    identity.add_auth_arguments(parser)

    args = parser.parse_args()
    if not args.schema:
        args.schema = sorted(DEFAULT_SCHEMA_DIR.glob("*.json"))
        if not args.schema:
            parser.error(f"{DEFAULT_SCHEMA_DIR} 에 *.json 이 없습니다. --schema 로 지정하세요.")
    if len(args.schema) > 1 and args.index_name:
        parser.error("--index-name 은 스키마 하나에만 적용할 수 있습니다")
    if args.auth in ("api-key", "access-token"):
        parser.error(f"--auth {args.auth} 는 이 스크립트에서 지원하지 않습니다. Entra ID 인증을 쓰세요.")
    return args


def load_jobs(args):
    """올릴 대상을 전부 먼저 읽는다.

    인덱스를 하나 만들어 놓고 두 번째 스키마의 오타 때문에 멈추는 것보다,
    아무것도 건드리기 전에 다 같이 실패하는 편이 낫다.
    """
    jobs = []
    for path in args.schema:
        if not path.is_file():
            raise SystemExit(f"스키마 파일이 없습니다: {path}")

        schema = load_schema(path)
        source = find_source(path)
        jobs.append({
            "schema": {**schema, "format": resolve_format(schema, source, str(path))},
            "index_name": f"{args.prefix}{args.index_name or schema['index']}",
            "source": source,
        })

    names = [job["index_name"] for job in jobs]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise SystemExit(f"인덱스 이름이 겹칩니다: {', '.join(duplicates)}")
    return jobs


def create_openai_client(args, credential):
    return AzureOpenAI(
        azure_endpoint=args.foundry_endpoint,
        azure_ad_token_provider=get_bearer_token_provider(credential, EMBEDDING_SCOPE),
        api_version=args.api_version,
    )


def run_job(job, args, credential, openai_client, index_client):
    schema, index_name = job["schema"], job["index_name"]
    print(f"==> {index_name} ({job['source'].name})")

    documents = build_documents(schema, job["source"])
    if not documents:
        print("    문서가 없습니다. 건너뜁니다.")
        return
    print(f"    문서 {len(documents)}건")

    if args.dry_run:
        print(f"    dry-run — 필드: {', '.join(sorted(documents[0]))}")
        return

    ensure_index(index_client, build_index(schema, index_name), args.recreate)
    embed_documents(openai_client, args.deployment, documents, schema["vector"])

    search_client = SearchClient(
        endpoint=args.search_endpoint, index_name=index_name, credential=credential
    )
    upload_documents(search_client, documents)
    print(f"    완료: {index_name}")


def main():
    args = parse_args()
    try:
        jobs = load_jobs(args)
    except SchemaError as error:
        sys.exit(f"스키마가 잘못되었습니다.\n  {error}")

    credential = identity.get_credential(args)
    openai_client = None if args.dry_run else create_openai_client(args, credential)
    index_client = None if args.dry_run else SearchIndexClient(
        endpoint=args.search_endpoint, credential=credential
    )

    for job in jobs:
        run_job(job, args, credential, openai_client, index_client)

    print()
    print("Foundry 에 지식으로 붙일 때는 프로젝트의 AI Search 연결을 고르고 위 인덱스 이름을")
    print("지정하면 됩니다. 의미 체계 구성 이름은 " + SEMANTIC_CONFIGURATION_NAME + " 입니다.")


if __name__ == "__main__":
    main()
