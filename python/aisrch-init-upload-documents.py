#!/usr/bin/env python3

"""실습용 Azure AI Search 인덱스를 만들고 문서를 넣는다. 점프박스에서 실행한다.

AI Search 는 공용 접근과 API 키가 둘 다 꺼져 있어, 닿을 수 있는 곳은 VNet 안뿐이다.
점프박스 관리 ID 가 Search Service Contributor(인덱스 정의)와 Search Index Data
Contributor(문서)를 갖고 있다.

인덱스 하나가 스키마 JSON 하나다. 어떤 필드를 만들고 원본의 어느 값을 넣을지는
전부 그 파일에 있고, 이 스크립트는 읽어서 만들고 임베딩해서 올리기만 한다.
스키마의 형태는 aisrch_schema.py 의 첫머리에 적어 두었다.

  python aisrch-init-upload-documents.py \\
      --search-endpoint https://<서비스명>.search.windows.net \\
      --foundry-endpoint https://<리소스>.cognitiveservices.azure.com \\
      --schema assets/tools/news.index.json \\
      --auth managed-identity
"""

import argparse
import sys
from pathlib import Path

from azure.core.exceptions import ResourceNotFoundError
from azure.identity import get_bearer_token_provider
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from openai import AzureOpenAI

import aisrch_schema
import identity
from aisrch_schema import SchemaError

# 한 번의 임베딩 호출에 넣는 텍스트 개수. 너무 크면 요청이 토큰 한도에 걸린다.
EMBEDDING_BATCH_SIZE = 16

# 한 번의 업로드 요청에 넣는 문서 개수.
UPLOAD_BATCH_SIZE = 100

# 실패한 문서를 보고할 때 몇 개까지 보여 줄지.
MAX_REPORTED_FAILURES = 10

# 임베딩은 데이터 평면 호출이라 고전 스코프를 쓴다.
EMBEDDING_SCOPE = "https://cognitiveservices.azure.com/.default"

DEFAULT_SCHEMA_DIR = Path(__file__).parent / "assets" / "tools"


def parse_args():
    parser = argparse.ArgumentParser(
        description="스키마 JSON 을 읽어 Azure AI Search 인덱스를 만들고 문서를 넣는다. "
                    "점프박스에서 실행한다.",
        epilog="AI Search 와 Foundry 둘 다 Private Endpoint 로만 열려 있다. VNet 밖에서 "
               "실행하면 이름 해석 단계에서 실패한다. 인증은 관리 ID 를 쓰므로 점프박스에서는 "
               "--auth managed-identity 가 가장 확실하다. --schema 를 생략하면 "
               f"{DEFAULT_SCHEMA_DIR.name} 안의 *.index.json 을 전부 올린다.",
    )
    parser.add_argument("--search-endpoint", required=True,
                        help="AI Search 엔드포인트, https://<서비스명>.search.windows.net")
    parser.add_argument("--foundry-endpoint", required=True,
                        help="임베딩을 호출할 Foundry 계정 엔드포인트")
    parser.add_argument("--deployment", default="text-embedding-3-large",
                        help="임베딩 모델 배포 이름")

    parser.add_argument("--schema", action="append", default=[], metavar="JSON", type=Path,
                        help="인덱스 정의 파일. 반복 지정할 수 있다.")
    parser.add_argument("--index-name", metavar="NAME",
                        help="스키마의 index 를 이 이름으로 덮어쓴다. --schema 가 하나일 때만 쓸 수 있다.")
    parser.add_argument("--source", metavar="PATH", type=Path,
                        help="스키마의 source 를 이 파일로 덮어쓴다. --schema 가 하나일 때만 쓸 수 있다.")
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
        args.schema = sorted(DEFAULT_SCHEMA_DIR.glob("*.index.json"))
        if not args.schema:
            parser.error(f"{DEFAULT_SCHEMA_DIR} 에 *.index.json 이 없습니다. --schema 로 지정하세요.")
    if len(args.schema) > 1 and (args.index_name or args.source):
        parser.error("--index-name 과 --source 는 스키마 하나에만 적용할 수 있습니다")
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

        schema = aisrch_schema.load_schema(path)
        source = args.source or aisrch_schema.source_path(schema, path)
        if not source.is_file():
            raise SystemExit(f"{path} 가 가리키는 원본 파일이 없습니다: {source}")

        jobs.append({
            "schema": schema,
            "index_name": f"{args.prefix}{args.index_name or schema['index']}",
            "source": source,
        })

    names = [job["index_name"] for job in jobs]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise SystemExit(f"인덱스 이름이 겹칩니다: {', '.join(duplicates)}")
    return jobs


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


def create_openai_client(args, credential):
    return AzureOpenAI(
        azure_endpoint=args.foundry_endpoint,
        azure_ad_token_provider=get_bearer_token_provider(credential, EMBEDDING_SCOPE),
        api_version=args.api_version,
    )


def run_job(job, args, credential, openai_client, index_client):
    schema, index_name = job["schema"], job["index_name"]
    print(f"==> {index_name} ({job['source'].name})")

    documents = aisrch_schema.build_documents(schema, job["source"])
    if not documents:
        print("    문서가 없습니다. 건너뜁니다.")
        return
    print(f"    문서 {len(documents)}건")

    if args.dry_run:
        print(f"    dry-run — 필드: {', '.join(sorted(documents[0]))}")
        return

    ensure_index(index_client, aisrch_schema.build_index(schema, index_name), args.recreate)
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
    print("지정하면 됩니다. 의미 체계 구성 이름은 " + aisrch_schema.SEMANTIC_CONFIGURATION_NAME + " 입니다.")


if __name__ == "__main__":
    main()
