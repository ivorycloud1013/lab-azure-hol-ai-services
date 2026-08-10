"""인덱스 정의와 문서 만들기를 JSON 스키마 한 벌로 기술한다.

aisrch-init-upload-documents.py 가 이 모듈을 쓴다. 원본 파일이 하나 늘 때마다
빌더 함수를 새로 쓰지 않아도 되도록, 어떤 필드를 만들고 원본의 어느 값을 넣을지를
전부 JSON 에 적는다. 스키마 한 벌이 인덱스 하나다.

  {
    "index": "news",
    "source": "news_ko.csv",
    "format": "csv",
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

필드 값은 from / key_from / split / point 중 하나로 지정한다. 자세한 형태는
아래 resolve_value 와 part_text 의 주석에 있다.
"""

import csv
import hashlib
import json
import re

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

# 청크 크기. 한국어는 한 글자가 대략 한 토큰에 가까우므로 문자 수로 잘라도 편차가 작다.
CHUNK_CHARACTERS = 1200
CHUNK_OVERLAP_CHARACTERS = 150

# 한국어 본문에 맞는 기본 분석기. 지정하지 않으면 공백 기준으로만 잘려 검색 품질이 떨어진다.
KOREAN_ANALYZER = "ko.microsoft"

VECTOR_PROFILE_NAME = "hnsw-profile"
VECTOR_ALGORITHM_NAME = "hnsw-config"
SEMANTIC_CONFIGURATION_NAME = "semantic-config"

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

SCHEMA_KEYS = {"index", "source", "format", "analyzer", "require", "chunk", "vector",
               "semantic", "fields"}
FIELD_KEYS = {"name", "type", "key", "searchable", "filterable", "facetable", "sortable",
              "analyzer", "separator", "shorten", *VALUE_KEYS}
PART_KEYS = {"column", "columns", "const", "label", "separator", "shorten", "first"}


class SchemaError(Exception):
    """스키마가 잘못되었을 때. 호출한 쪽이 파일 이름을 붙여 보고한다."""


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
    만든 조각의 경계에 있던 공백까지 사라져, 조각을 다시 이어 붙일 수 없게 된다.
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
        path_text = " > ".join(heading_stack) if heading_stack else "문서 시작"
        records.append({
            "heading": heading_stack[-1] if heading_stack else "문서 시작",
            "heading_path": path_text,
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
    longitude, latitude = record_text(record, spec["longitude"]), record_text(record, spec["latitude"])
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

    text = record_text(record, chunk["from"])
    pieces = chunk_text(text, chunk.get("size", CHUNK_CHARACTERS),
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


def build_documents(schema, source_path):
    """원본 파일 하나를 인덱스에 올릴 문서 목록으로 바꾼다."""
    required = schema.get("require", [])
    documents = []

    for index, record in enumerate(READERS[schema["format"]](source_path)):
        if any(not record_text(record, column) for column in required):
            continue
        record = {**record, "_source": source_path.name, "_index": index}
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

    if "from" in spec:
        for part in as_parts(spec["from"]):
            validate_part(part, where)
    if "key_from" in spec:
        for part in as_parts(spec["key_from"]):
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

    for key in ("index", "source", "format", "fields", "vector"):
        if not schema.get(key):
            raise SchemaError(f"{where} 에 {key} 가 없습니다")
    if schema["format"] not in READERS:
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
    """스키마를 읽고 검사한다. 원본 경로는 스키마 파일이 있는 곳을 기준으로 푼다."""
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise SchemaError(f"{path} 를 읽지 못했습니다: {error}") from None

    validate_schema(schema, str(path))
    return schema


def source_path(schema, path):
    return (path.parent / schema["source"]).resolve()
