"""모든 단계가 공유하는 인자와 client 배선.

여기 있는 것은 하네스가 아니다. 안 그러면 파일 여섯 개에 그대로 복사되어, 각 단계가
가르치려는 레이어보다 덩치가 커 보이게 만들 보일러플레이트다. 각 단계가 소유하는 것은
자기 파일의 INSTRUCTIONS 아래에 있고, 여기 있는 것은 모델에 닿는 방법뿐이다.
"""

import argparse
import os
import sys

PYTHON_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PYTHON_DIR not in sys.path:
    sys.path.insert(0, PYTHON_DIR)

from openai import OpenAI  # noqa: E402

import golden  # noqa: E402
import identity  # noqa: E402

DEFAULT_DEPLOYMENT = "gpt-5.6-terra"


def build_parser(description, epilog=None):
    """모든 단계가 출발하는 인자 골격. 이 랩의 다른 스크립트와 같은 순서를 쓴다 —
    endpoint, deployment, 인증, 그다음이 단계 고유 인자."""
    parser = argparse.ArgumentParser(description=description, epilog=epilog)
    parser.add_argument("--endpoint", required=True, help="Foundry project 엔드포인트")
    parser.add_argument("--deployment", default=DEFAULT_DEPLOYMENT, help="모델 deployment 이름")

    identity.add_auth_arguments(parser)

    parser.add_argument("--corpus", default=golden.DEFAULT_CORPUS, metavar="DIR",
                        help="질문의 근거가 되는 markdown 문서들이 있는 디렉터리")
    parser.add_argument("--questions", type=int, default=len(golden.GOLDEN),
                        help=f"골든 세트 {len(golden.GOLDEN)}문항 중 앞에서부터 몇 개를 물을지")
    parser.add_argument("--show-tools", action="store_true",
                        help="agent 가 부른 tool 과 인자를 그대로 출력")
    return parser


def finish_parsing(parser):
    """모든 단계가 하는 검증. 단계 고유 검증은 이 뒤에 붙인다."""
    args = parser.parse_args()
    if not os.path.isdir(args.corpus):
        parser.error(f"{args.corpus} 를 찾을 수 없습니다")
    if not 1 <= args.questions <= len(golden.GOLDEN):
        parser.error(f"--questions 는 1 에서 {len(golden.GOLDEN)} 사이여야 합니다")
    return args


def create_client(args):
    # v1 API: AzureOpenAI 도 api-version 도 없는 순정 OpenAI client 를 쓴다.
    # api_key 에 callable 을 주면 그게 token provider 이고, client 가 요청마다 갱신한다.
    if args.auth == "api-key":
        api_key = args.api_key
    elif args.auth == "access-token":
        api_key = args.access_token
    else:
        api_key = identity.get_token_provider(args)
    return OpenAI(base_url=args.endpoint.rstrip("/") + "/openai/v1/", api_key=api_key)


def prepare(args):
    """코퍼스와 질문을 한 번만 읽어, 각 단계가 들고 다닐 context 를 만든다.

    corpus 는 문서 이름 → {path, lines} 다. 검색이 무엇을 훑을지, 인용된 문서를 어디서
    읽을지가 전부 여기서 나온다. **어느 edition 이 유효한지는 여기 없다** — 그건 golden 의
    EDITIONS 이고, step 2 의 검증만 읽는다.
    """
    corpus = golden.load_corpus(args.corpus)
    if not corpus:
        raise SystemExit(f"{args.corpus} 에 markdown 문서가 없습니다")
    return {
        "client": create_client(args),
        "args": args,
        "corpus": corpus,
        "golden": golden.questions(args.questions),
    }
