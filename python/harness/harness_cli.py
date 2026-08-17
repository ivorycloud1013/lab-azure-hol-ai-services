"""Arguments and client wiring shared by every step.

None of this is the harness. It is the boilerplate that would otherwise be copied into
six files and make each step look bigger than the layer it teaches. What each step owns
is below its own INSTRUCTIONS constant; what is here is only how to reach the model.
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
    """The argument skeleton every step starts from, in the order the lab's other
    scripts use it: endpoint, deployment, auth, then whatever the step adds."""
    parser = argparse.ArgumentParser(description=description, epilog=epilog)
    parser.add_argument("--endpoint", required=True, help="Foundry project endpoint")
    parser.add_argument("--deployment", default=DEFAULT_DEPLOYMENT, help="model deployment name")

    identity.add_auth_arguments(parser)

    parser.add_argument("--file", default=golden.DEFAULT_DOCUMENT, metavar="MD",
                        help="markdown document the questions are about")
    parser.add_argument("--questions", type=int, default=len(golden.GOLDEN),
                        help=f"how many of the {len(golden.GOLDEN)} questions to ask")
    parser.add_argument("--show-tools", action="store_true",
                        help="print every tool call the agent makes, with its arguments")
    return parser


def finish_parsing(parser):
    """Validate what every step validates. Steps add their own checks after this."""
    args = parser.parse_args()
    if not os.path.isfile(args.file):
        parser.error(f"{args.file} not found")
    if not 1 <= args.questions <= len(golden.GOLDEN):
        parser.error(f"--questions must be between 1 and {len(golden.GOLDEN)}")
    return args


def create_client(args):
    # v1 API: the stock OpenAI client, no AzureOpenAI and no api-version.
    # A callable api_key is the token provider, which the client refreshes per request.
    if args.auth == "api-key":
        api_key = args.api_key
    elif args.auth == "access-token":
        api_key = args.access_token
    else:
        api_key = identity.get_token_provider(args)
    return OpenAI(base_url=args.endpoint.rstrip("/") + "/openai/v1/", api_key=api_key)


def prepare(args):
    """Load the document and the questions once, and hand back the context steps pass around."""
    document, lines = golden.load_document(args.file)
    return {
        "client": create_client(args),
        "args": args,
        "document": document,
        "lines": lines,
        "golden": golden.resolve_golden(lines, args.questions),
    }
