#!/usr/bin/env python3

import argparse
import json
import mimetypes
import os
import time
import uuid

from azure.ai.contentunderstanding import ContentUnderstandingClient
from azure.ai.contentunderstanding.models import (
    AnalysisInput,
    ContentAnalyzer,
    ContentAnalyzerConfig,
    ContentFieldSchema,
)
from azure.core.credentials import AccessToken, AzureKeyCredential
from azure.core.exceptions import HttpResponseError

import identity

# The client asks the credential for https://cognitiveservices.azure.com/.default
# on its own, so identity.get_token_provider (ai.azure.com) does not apply here.
DEFAULT_ANALYZER = "prebuilt-document"

# Custom analyzers derive from one of four base analyzers, one per modality.
# This script reads documents, so the document one is the only choice.
BASE_ANALYZER = "prebuilt-document"

STATIC_TOKEN_LIFETIME_SECONDS = 3600


def parse_args():
    parser = argparse.ArgumentParser(
        description="Extract markdown and fields from documents with Foundry Content Understanding.",
        epilog="Analyzers that summarize or extract fields run on the model deployments registered "
               "on the account. --analyzer prebuilt-layout needs none.",
    )
    parser.add_argument("--endpoint", required=True, help="Foundry account endpoint")

    identity.add_auth_arguments(parser)

    parser.add_argument("--file", action="append", default=[], required=True, metavar="PATH",
                        help="document to analyze, repeat for more")
    parser.add_argument("--analyzer", help=f"existing analyzer id (default {DEFAULT_ANALYZER})")
    parser.add_argument("--schema", metavar="JSON",
                        help='fieldSchema file — {"name": ..., "fields": {"Title": {"type": "string", '
                             '"method": "extract", "description": ...}}} — builds a custom analyzer for this run')
    parser.add_argument("--processing-location", choices=["geography", "dataZone", "global"],
                        help="where the service may process the data (default global)")
    parser.add_argument("--out-dir", help="directory for the .md and .json results (default: beside each document)")
    parser.add_argument("--api-version", default="2025-11-01")

    args = parser.parse_args()
    if args.analyzer and args.schema:
        parser.error("--analyzer picks an existing analyzer and --schema builds one, pass only one")
    for path in args.file:
        if not os.path.isfile(path):
            parser.error(f"{path} not found")
    return args


class StaticTokenCredential:
    """Wraps a token the caller already holds so the SDK can treat it as a credential."""

    def __init__(self, token):
        self.token = token

    def get_token(self, *_scopes, **_kwargs):
        # The real expiry is not in our hands, so claim an hour and let a 401 speak.
        return AccessToken(self.token, int(time.time()) + STATIC_TOKEN_LIFETIME_SECONDS)


def create_client(args):
    if args.auth == "api-key":
        credential = AzureKeyCredential(args.api_key)
    elif args.auth == "access-token":
        credential = StaticTokenCredential(args.access_token)
    else:
        credential = identity.get_credential(args)
    return ContentUnderstandingClient(
        endpoint=args.endpoint,
        credential=credential,
        api_version=args.api_version,
    )


def build_input(path):
    """Documents travel inline, so the lab needs no blob storage. The SDK base64s them."""
    mime_type, _ = mimetypes.guess_type(path)
    with open(path, "rb") as document:
        data = document.read()
    return AnalysisInput(
        name=os.path.basename(path),
        mime_type=mime_type or "application/octet-stream",
        data=data,
    )


def build_jobs(args):
    """Pair every document with the path prefix its .md and .json are written to."""
    prefixes = unique_prefixes([output_prefix(args, path) for path in args.file])
    return list(zip(prefixes, [build_input(path) for path in args.file]))


def output_prefix(args, path):
    directory = args.out_dir or os.path.dirname(path) or "."
    return os.path.join(directory, os.path.splitext(os.path.basename(path))[0])


def unique_prefixes(prefixes):
    """Two documents landing on the same name must not overwrite each other's results."""
    taken, unique = set(), []
    for prefix in prefixes:
        candidate, suffix = prefix, 2
        while candidate in taken:
            candidate, suffix = f"{prefix}-{suffix}", suffix + 1
        taken.add(candidate)
        unique.append(candidate)
    return unique


def load_field_schema(path):
    with open(path, encoding="utf-8") as schema_file:
        field_schema = json.load(schema_file)
    if not isinstance(field_schema, dict) or not isinstance(field_schema.get("fields"), dict):
        raise SystemExit(f"{path} must be a fieldSchema object holding a fields map")
    return ContentFieldSchema(field_schema)


def create_analyzer(client, args, analyzer_id):
    analyzer = ContentAnalyzer(
        description=f"HOL analyzer from {os.path.basename(args.schema)}",
        base_analyzer_id=BASE_ANALYZER,
        field_schema=load_field_schema(args.schema),
        # Grounding tells you which part of the document each value came from.
        config=ContentAnalyzerConfig(estimate_field_source_and_confidence=True),
    )
    client.begin_create_analyzer(analyzer_id, analyzer).result()


def analyze(client, args, analyzer_id, analysis_input):
    # One input per call. Several inputs in a single call is a pro mode feature.
    poller = client.begin_analyze(
        analyzer_id,
        inputs=[analysis_input],
        processing_location=args.processing_location,
    )
    return poller.result()


def save_result(result, prefix):
    directory = os.path.dirname(prefix)
    if directory:
        os.makedirs(directory, exist_ok=True)

    json_path = f"{prefix}.json"
    with open(json_path, "w", encoding="utf-8") as out:
        # default=str catches what as_dict leaves as objects, such as warnings.
        json.dump(result.as_dict(), out, ensure_ascii=False, indent=2, default=str)

    markdown_path = f"{prefix}.md"
    with open(markdown_path, "w", encoding="utf-8") as out:
        out.write("\n\n".join(content.markdown or "" for content in result.contents or []))
    return markdown_path, json_path


def get_field_value(field):
    """Every field type carries its value under its own value* key.

    as_dict() rather than the typed model, because array and object fields nest
    further field models that do not print or serialize on their own.
    """
    for key, value in field.as_dict().items():
        if key.startswith("value"):
            return value
    return None


def format_value(value):
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def print_summary(result, markdown_path, json_path):
    print(f"{markdown_path}  {json_path}")
    for content in result.contents or []:
        for name, field in sorted((content.fields or {}).items()):
            print(f"  {name}: {format_value(get_field_value(field))}")
    for warning in result.warnings or []:
        print(f"  warning — {warning.code}: {warning.message}")


def analyze_all(client, args, analyzer_id):
    for prefix, analysis_input in build_jobs(args):
        result = analyze(client, args, analyzer_id, analysis_input)
        print_summary(result, *save_result(result, prefix))


def main():
    args = parse_args()
    client = create_client(args)

    try:
        if not args.schema:
            analyze_all(client, args, args.analyzer or DEFAULT_ANALYZER)
            return

        # The custom analyzer only exists for this run, so it gets a throwaway name
        # and is deleted again — otherwise every run would leave one behind.
        analyzer_id = f"hol-cu-{uuid.uuid4().hex[:8]}"
        create_analyzer(client, args, analyzer_id)
        print(f"analyzer {analyzer_id} ready")
        try:
            analyze_all(client, args, analyzer_id)
        finally:
            client.delete_analyzer(analyzer_id)
    except HttpResponseError as error:
        raise SystemExit(f"{error.status_code}: {error.message or error.reason}")
    finally:
        client.close()


if __name__ == "__main__":
    main()
