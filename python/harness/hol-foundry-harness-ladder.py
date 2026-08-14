#!/usr/bin/env python3
"""Run the same questions through five harness levels and score every one.

The model, the questions and the document never change between levels. Only the
harness does. L2 and L3 in particular call the very same grep over the very same
file — they differ in how the tools are described and in what a failed call says
back. If the scoreboard shows L3 finishing in fewer turns, the description is the
only thing that could have done it, and that is the whole lesson here.

    L0  prompt only            the model has to make it up
    L1  whole document inline  it answers, and pays for the document every question
    L2  grep + read tools      tokens collapse, round trips grow
    L3  the same tools, described properly and failing usefully
    L4  L3 plus a critic that checks the cited lines against the file
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from typing import Literal

# harness/ is a subdirectory, so identity.py is one level up. Every other script in
# this lab is a sibling of identity.py and never had to think about it; this one does,
# and without this line the import below fails outright.
PYTHON_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PYTHON_DIR)

from openai import OpenAI  # noqa: E402
from pydantic import BaseModel  # noqa: E402

import identity  # noqa: E402

BANNER = "=" * 72
RULE = "-" * 72

DEFAULT_DOCUMENT = os.path.join(PYTHON_DIR, "assets", "tools", "KB주택시장리뷰_2025년 10월호.md")

# L0 and L1 share these words on purpose. If the two levels also differed in how they
# were told to behave, the jump in the hit column could be credited to the wording
# instead of to the document, and the first rung of the ladder would prove nothing.
INSTRUCTIONS = (
    "You answer questions about one Korean housing market report. "
    "Cite every figure you report as [line N], the line it came from. "
    "Say you could not find it rather than guessing. Answer in the language of the question."
)

INSTRUCTIONS_TOOLS = f"{INSTRUCTIONS} Always search before answering."

CRITIC_INSTRUCTIONS = (
    "You check an answer against the document lines it cited. The evidence below was "
    "read from the file, not copied from the answer — if a figure in the answer is not "
    "in the evidence, the answer is wrong no matter how plausible it sounds. "
    "Reply pass only when every figure in the answer appears in the evidence."
)

# Same ceiling as hol-foundry-agents-responses.py. The ladder compares tool design,
# not loop budget, so giving one level more rounds than another would let a level win
# by being allowed to flail longer.
MAX_TOOL_ROUNDS = 8

# One rejection. A second almost never flips a verdict that two rounds could not fix,
# and it doubles the most expensive level in the ladder.
MAX_CRITIC_ROUNDS = 2

MAX_MATCHES = 40
CONTEXT_LINES = 2
MAX_CONTEXT_LINES = 10
MAX_PATTERNS = 8
MAX_READ_LINES = 200
GREP_NO_MATCH_RETURNCODE = 1

# L1 puts the entire file in one request. Past this the request stops fitting and only
# L1 fails, leaving a hole in the middle of the ladder that looks like a bug in the
# level rather than a limit of the approach. Guards --file, not the default document.
MAX_DOCUMENT_CHARS = 120_000

# azure-ai-evaluation defaults to 2024-02-15-preview, which recent deployments reject.
# Setting it here is what keeps the scorer working when the judge deployment moves.
JUDGE_API_VERSION = "2024-12-01-preview"

# The library's own default. Repeated here because the scoreboard prints pass/fail
# alongside the score, and a threshold you cannot see is a threshold you cannot argue with.
EVALUATOR_THRESHOLD = 3

# Lines of file either side of a cited line, handed to the critic as evidence.
EVIDENCE_CONTEXT = 1

CITATION_RE = re.compile(r"\[line\s*(\d+)\]")

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
        # The Seoul figure is not in the prose at all — it lives in a chart's alt text and
        # in the chart's JSON. A model skimming the narrative misses it; a model that
        # searches finds it. That gap is the point of keeping this question in the set.
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

# The bad tools. Same grep underneath, four things wrong on top: one pattern instead of
# many, no word about the document being Korean, argument names that say nothing, and no
# additionalProperties. Every one of those is a mistake people actually ship.
TOOLS_TERSE = [
    {
        "type": "function",
        "name": "grep",
        "description": "search the file",
        "parameters": {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        },
    },
    {
        "type": "function",
        "name": "read",
        "description": "read the file",
        "parameters": {
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
        },
    },
]

# The good tools, lifted from hol-foundry-agents-responses.py. Patterns are a list so one
# call can try several spellings, the description says which language to search in, the
# integer arguments say what they count, and the names explain themselves.
TOOLS_RICH = [
    {
        "type": "function",
        "name": "search_document",
        "description": "Search the document for several regular expressions at once. "
                       "Returns matching lines as line:text with surrounding context. "
                       "Search in the language the document is written in.",
        "parameters": {
            "type": "object",
            "properties": {
                "patterns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "One or more extended regular expressions, "
                                   "e.g. ['전세수급지수', '청약|경쟁률']. "
                                   "A line matching any of them is returned.",
                },
                "context_lines": {
                    "type": "integer",
                    "description": f"how many lines to show before and after each hit, 0 to {MAX_CONTEXT_LINES}",
                },
            },
            "required": ["patterns", "context_lines"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "read_lines",
        "description": "Read a line range from the document, "
                       "to see the full context around a search hit.",
        "parameters": {
            "type": "object",
            "properties": {
                "start_line": {"type": "integer", "description": "1-based first line to read"},
                "line_count": {"type": "integer", "description": f"how many lines, at most {MAX_READ_LINES}"},
            },
            "required": ["start_line", "line_count"],
            "additionalProperties": False,
        },
    },
]


class Verdict(BaseModel):
    verdict: Literal["pass", "revise"]
    reason: str


class LocalScore(BaseModel):
    groundedness: int
    relevance: int
    reason: str


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run one question set through five harness levels and score each level.",
        epilog="Scoring costs more than the agents do — four evaluators run per row. "
               "Try --level L3 --questions 2 before spending a full ladder.",
    )
    parser.add_argument("--endpoint", required=True, help="Foundry project endpoint")
    parser.add_argument("--deployment", default="gpt-5.6-terra", help="model deployment name")

    identity.add_auth_arguments(parser)

    parser.add_argument("--file", default=DEFAULT_DOCUMENT, metavar="MD",
                        help="markdown document the questions are about")
    parser.add_argument("--level", choices=["L0", "L1", "L2", "L3", "L4", "all"], default="all")
    parser.add_argument("--questions", type=int, default=len(GOLDEN),
                        help=f"how many of the {len(GOLDEN)} questions to use")
    parser.add_argument("--repeat", type=int, default=1,
                        help="how many times to ask each question, to blunt run-to-run luck")
    parser.add_argument("--judge", choices=["evaluation", "llm", "none"], default="evaluation",
                        help="azure-ai-evaluation, an inline rubric, or no judge at all")
    parser.add_argument("--judge-deployment", default="gpt-4.1", help="model that scores the answers")
    parser.add_argument("--judge-reasoning", action="store_true",
                        help="the judge deployment is a reasoning model, so drop temperature")
    parser.add_argument("--no-upload", dest="upload", action="store_false",
                        help="score locally without publishing to the Foundry Evaluation tab")
    parser.add_argument("--out-dir", default=None, help="where to leave the per-row jsonl")
    parser.add_argument("--show-tools", action="store_true", help="print each search the agent runs")

    args = parser.parse_args()
    if not os.path.isfile(args.file):
        parser.error(f"{args.file} not found")
    if not 1 <= args.questions <= len(GOLDEN):
        parser.error(f"--questions must be between 1 and {len(GOLDEN)}")
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    # Uploading needs the project endpoint. Catching it here costs nothing; catching it
    # after the judge has run costs the entire scoring bill for a result that cannot land.
    if args.judge == "evaluation" and args.upload and "/api/projects/" not in args.endpoint:
        parser.error("--endpoint must be a project endpoint (.../api/projects/<name>) to upload, "
                     "or pass --no-upload")
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


def account_endpoint(url):
    """Strip the project path off, leaving the account root.

    One --endpoint has to serve two callers that disagree. evaluate() uploads to the
    project endpoint, but the evaluators reach their judge model over the classic
    AzureOpenAI path, which wants the account. Deriving one from the other keeps this
    script to a single endpoint argument like every other script in the lab.
    """
    marker = "/api/projects/"
    base = url.split(marker)[0] if marker in url else url
    return base.rstrip("/")


def load_document(path):
    """Read the file once and hand back both forms it gets used in.

    Every level reads this, several times per question. Reading it per call would put
    disk time into the seconds column, which is meant to measure the harness.
    """
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    return text, text.splitlines()


def resolve_golden(lines, count, repeat):
    """Slice each question's supporting text out of the document instead of storing it.

    Copying those paragraphs into GOLDEN would be easier to read and quietly wrong: the
    moment the document is re-extracted, the copy drifts, and from then on the judge
    scores answers against sentences that no longer exist in the file the agent is
    searching. Every level would keep reporting numbers, and every number would be a lie.
    """
    resolved = []
    for item in GOLDEN[:count]:
        excerpt = []
        for first, last in item["source_lines"]:
            excerpt += [f"{n}: {lines[n - 1]}" for n in range(first, last + 1) if n <= len(lines)]
        context = "\n".join(excerpt)
        for round_number in range(1, repeat + 1):
            identifier = item["id"] if repeat == 1 else f"{item['id']}#{round_number}"
            resolved.append({**item, "id": identifier, "context": context})
    return resolved


def run_command(command):
    """No shell, so a pattern from the model cannot turn into a second command."""
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode > GREP_NO_MATCH_RETURNCODE:
        return f"command failed: {result.stderr.strip()}"
    return result.stdout


def search_document(path, patterns, context_lines=CONTEXT_LINES):
    """Match any of the patterns in one pass. One call with several patterns beats
    several calls, because the hits come back interleaved in document order."""
    kept = [p for p in patterns if p][:MAX_PATTERNS]
    if not kept:
        return "no pattern given"
    command = ["grep", "--line-number", "--ignore-case", "--extended-regexp",
               f"--context={max(0, min(context_lines, MAX_CONTEXT_LINES))}"]
    for pattern in kept:
        command += ["-e", pattern]

    output = run_command(command + ["--", path])
    lines = output.splitlines()
    if not lines:
        return f"no match for {' | '.join(kept)}"
    if len(lines) > MAX_MATCHES:
        return "\n".join(lines[:MAX_MATCHES]) + (
            f"\n… {len(lines) - MAX_MATCHES} more lines, narrow the patterns "
            "or lower context_lines")
    return output


def read_lines(path, start_line, line_count):
    start = max(start_line, 1)
    end = start + min(line_count, MAX_READ_LINES) - 1
    return run_command(["sed", "-n", f"{start},{end}p", path])


def call_tool_terse(path, name, arguments):
    """The dispatcher that explains nothing.

    A miss and a malformed call come back the same way, so the model cannot tell which
    of the two just happened and has no reason to believe a different next attempt would
    do better. What it does instead is run the same search again under another spelling
    until the round budget is gone. That behaviour is L2's whole failure mode, and it is
    produced here, not by any weakness in grep.
    """
    try:
        if name == "grep":
            return search_document(path, [arguments["q"]], CONTEXT_LINES)
        if name == "read":
            return read_lines(path, arguments["a"], arguments["b"])
        return "error"
    except (KeyError, TypeError, ValueError):
        return "error"


def call_tool_rich(path, name, arguments):
    """The dispatcher that answers three failures three different ways.

    A miss repeats what was searched for, a malformed call names the argument and the
    reason, and a truncated result says how to narrow it. Letting the exception escape
    would kill the run; returning an empty string would keep the run alive but leave the
    model with nothing to act on. The difference between those three replies and one
    silent one is the entire distance between L2 and L3.
    """
    try:
        if name == "search_document":
            return search_document(path, arguments["patterns"],
                                   arguments.get("context_lines", CONTEXT_LINES))
        if name == "read_lines":
            return read_lines(path, arguments["start_line"], arguments["line_count"])
        return f"unknown tool {name}"
    except (KeyError, TypeError, ValueError) as error:
        return f"bad arguments for {name}: {error}"


def blank_run():
    """The counters a level accumulates. Kept in one dict so L4 can carry them across
    critic rounds instead of resetting them and under-reporting its own cost."""
    return {
        "text": "", "response_id": None, "input_tokens": 0, "output_tokens": 0,
        "cached_tokens": 0, "turns": 0, "tool_calls": 0,
        "tool_calls_detail": [], "tool_definitions": [],
    }


def add_usage(run, usage):
    """Fold one response's usage into a new run dict.

    cached_tokens is nested and absent on deployments that do not cache, so it is read
    defensively — a missing attribute here would end a run that was otherwise fine, and
    the cached column is the least important thing on the board.
    """
    details = getattr(usage, "input_tokens_details", None)
    cached = getattr(details, "cached_tokens", 0) or 0 if details is not None else 0
    return {
        **run,
        "input_tokens": run["input_tokens"] + (usage.input_tokens or 0),
        "output_tokens": run["output_tokens"] + (usage.output_tokens or 0),
        "cached_tokens": run["cached_tokens"] + cached,
        "turns": run["turns"] + 1,
    }


def collect_tool_calls(args, response, path, dispatch):
    """Answer every function call in this response. Returns ([], []) when the agent is done."""
    outputs, recorded = [], []
    for item in response.output:
        if item.type != "function_call":
            continue
        arguments = json.loads(item.arguments)
        if args.show_tools:
            print(f"    [{item.name} {json.dumps(arguments, ensure_ascii=False)}]")
        outputs.append({
            "type": "function_call_output",
            "call_id": item.call_id,
            "output": dispatch(path, item.name, arguments),
        })
        # ToolCallAccuracyEvaluator wants the calls in this shape. Recording them as they
        # happen is the only chance — the response objects are not kept.
        recorded.append({"type": "tool_call", "name": item.name, "arguments": arguments})
    return outputs, recorded


def tool_loop(ctx, run, tools, dispatch, instructions, prompt):
    """Drive one prompt to an answer, folding the cost into the run it was handed.

    Takes a run and returns a new one rather than starting fresh, because L4 comes back
    through here after a rejection and its turns and tokens have to keep adding up.
    """
    args = ctx["args"]
    request = {"model": args.deployment, "input": prompt}
    if run["response_id"]:
        # previous_response_id carries the history, so only the new turn goes up.
        request["previous_response_id"] = run["response_id"]
    else:
        request["instructions"] = instructions
    if tools:
        request["tools"] = tools

    response = ctx["client"].responses.create(**request)
    run = add_usage(run, response.usage)

    for _ in range(MAX_TOOL_ROUNDS):
        if not tools:
            break
        outputs, recorded = collect_tool_calls(args, response, args.file, dispatch)
        if not outputs:
            break
        run = {**run, "tool_calls": run["tool_calls"] + len(recorded),
               "tool_calls_detail": run["tool_calls_detail"] + recorded}
        response = ctx["client"].responses.create(
            model=args.deployment, previous_response_id=response.id,
            input=outputs, tools=tools)
        run = add_usage(run, response.usage)
    else:
        # Out of rounds with tool calls still pending. Not fatal — a level that flails is
        # a result worth scoring, and killing the process here would lose every row
        # already paid for in this level.
        print(f"    [gave up after {MAX_TOOL_ROUNDS} tool rounds]")

    return {**run, "text": response.output_text, "response_id": response.id,
            "tool_definitions": tools or []}


def cited_lines(text):
    """Pull the [line N] citations out of an answer. No model involved — a citation that
    was invented is caught by reading the file, not by asking anyone's opinion."""
    seen = []
    for match in CITATION_RE.finditer(text):
        number = int(match.group(1))
        if number not in seen:
            seen.append(number)
    return seen


def gather_evidence(lines, numbers):
    """Read the cited lines back out of the file.

    Handing the critic the whole document would work and would also hand L4 the token
    bill L1 exists to warn about. Handing it only what the answer claims to rely on keeps
    the check cheap, and a citation pointing past the end of the file is already caught
    right here, before the critic is even called.
    """
    excerpt = []
    for number in numbers:
        if not 1 <= number <= len(lines):
            excerpt.append(f"{number}: (no such line in the document)")
            continue
        first = max(1, number - EVIDENCE_CONTEXT)
        last = min(len(lines), number + EVIDENCE_CONTEXT)
        excerpt += [f"{n}: {lines[n - 1]}" for n in range(first, last + 1)]
    return "\n".join(excerpt)


def critique(ctx, item, answer, evidence):
    """Ask for a verdict in a fixed shape.

    A free-text critic answers "대체로 맞는 것 같은데…" and then something has to decide
    what that meant. Forcing the schema moves that decision to the model, which is the
    only party that actually knows.
    """
    response = ctx["client"].responses.parse(
        model=ctx["args"].deployment,
        instructions=CRITIC_INSTRUCTIONS,
        input=f"질문: {item['question']}\n\n답변:\n{answer}\n\n인용된 줄:\n{evidence}",
        text_format=Verdict,
    )
    return response.output_parsed, response.usage


def l0_prompt_only(ctx, item):
    """L0 prompt only. No document, no tools — the same instructions as L1, so whatever
    changes on the next rung is the document and nothing else."""
    return tool_loop(ctx, blank_run(), None, None, INSTRUCTIONS, item["question"])


def l1_whole_document(ctx, item):
    """L1 the whole document in the request. It answers, and it pays for all 810 lines
    again on every single question."""
    prompt = f"{ctx['document']}\n\n---\n질문: {item['question']}"
    return tool_loop(ctx, blank_run(), None, None, INSTRUCTIONS, prompt)


def l2_terse_tools(ctx, item):
    """L2 grep and read, badly described. One pattern per call, no hint that the document
    is Korean, arguments called a and b, and failures that all look alike."""
    return tool_loop(ctx, blank_run(), TOOLS_TERSE, call_tool_terse,
                     INSTRUCTIONS_TOOLS, item["question"])


def l3_rich_tools(ctx, item):
    """L3 the same grep, described properly. Pattern lists, the document's language named,
    self-explaining arguments, and errors the model can act on."""
    return tool_loop(ctx, blank_run(), TOOLS_RICH, call_tool_rich,
                     INSTRUCTIONS_TOOLS, item["question"])


def l4_verified(ctx, item):
    """L4 L3 plus verification. The cited lines are read from the file and a critic rules
    on them; a rejection goes back to the same conversation. Roughly twice L3's cost, and
    the board is meant to show that verification is not free."""
    run = tool_loop(ctx, blank_run(), TOOLS_RICH, call_tool_rich,
                    INSTRUCTIONS_TOOLS, item["question"])
    for _ in range(MAX_CRITIC_ROUNDS):
        numbers = cited_lines(run["text"])
        if not numbers:
            feedback = "인용한 줄번호가 없습니다. 근거를 [line N] 형식으로 달아 다시 답해 주세요."
        else:
            verdict, usage = critique(ctx, item, run["text"], gather_evidence(ctx["lines"], numbers))
            run = add_usage(run, usage)
            if verdict.verdict == "pass":
                break
            if ctx["args"].show_tools:
                print(f"    [critic revise] {verdict.reason}")
            feedback = f"검토 결과 수정이 필요합니다: {verdict.reason}\n다시 확인하고 답해 주세요."
        run = tool_loop(ctx, run, TOOLS_RICH, call_tool_rich, INSTRUCTIONS_TOOLS, feedback)
    return run


LEVELS = {
    "L0": l0_prompt_only,
    "L1": l1_whole_document,
    "L2": l2_terse_tools,
    "L3": l3_rich_tools,
    "L4": l4_verified,
}


def is_hit(item, text):
    """Every key must appear literally. Deterministic, free, and the one column that
    survives when there is no judge — which is why the board leads with it."""
    return all(key in text for key in item["answer_key"])


def run_level(ctx, name, position, golden):
    level = LEVELS[name]
    print(f"\n{BANNER}")
    print(f"{position} {name}")
    print(" ".join(level.__doc__.split()))  # one line, however the docstring wraps
    print(BANNER)

    records = []
    for item in golden:
        started = time.perf_counter()
        run = level(ctx, item)
        elapsed = time.perf_counter() - started
        hit = is_hit(item, run["text"])
        print(f"  {'hit ' if hit else 'miss'} {item['id']:<28} "
              f"{run['turns']} turns  {run['tool_calls']} tools  "
              f"{run['input_tokens']:,} in  {elapsed:.1f}s")
        records.append({
            "id": item["id"],
            "query": item["question"],
            "response": run["text"],
            "context": item["context"],
            "ground_truth": " / ".join(item["answer_key"]),
            "tool_calls": run["tool_calls_detail"],
            "tool_definitions": run["tool_definitions"],
            "hit": hit,
            "input_tokens": run["input_tokens"],
            "output_tokens": run["output_tokens"],
            "cached_tokens": run["cached_tokens"],
            "turns": run["turns"],
            "tool_call_count": run["tool_calls"],
            "seconds": round(elapsed, 2),
        })
    return records


def write_jsonl(records, out_dir, name):
    """evaluate() reads from a path, not from memory, so the rows have to hit disk.

    They land outside the repo by default. Writing them next to the script would drop
    generated files into a tree whose .gitignore this lab does not own.
    """
    path = os.path.join(out_dir, f"{name}.jsonl")
    with open(path, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def import_evaluation():
    """Import azure-ai-evaluation, or return None and let the ladder continue without it.

    Scoring is the point of this lab, but it is not the only measurement in it. By the
    time this runs the hit, token and turn columns are already recorded, and those carry
    most of the lesson. A version skew in a transitive dependency should cost the grades,
    not the run.
    """
    try:
        from azure.ai.evaluation import (  # noqa: PLC0415
            GroundednessEvaluator,
            IntentResolutionEvaluator,
            RelevanceEvaluator,
            ToolCallAccuracyEvaluator,
            evaluate,
        )
    except Exception as error:  # noqa: BLE001 — any import-time failure downgrades the judge
        print(f"\n[azure-ai-evaluation unavailable, falling back to --judge llm: {error}]")
        return None
    return {
        "evaluate": evaluate,
        "classes": {
            "groundedness": GroundednessEvaluator,
            "relevance": RelevanceEvaluator,
            "intent_resolution": IntentResolutionEvaluator,
            "tool_call_accuracy": ToolCallAccuracyEvaluator,
        },
    }


def build_evaluators(args, module, credential):
    """Four evaluators over one judge deployment, plus the column mapping each one wants."""
    config = {
        "azure_endpoint": account_endpoint(args.endpoint),
        "azure_deployment": args.judge_deployment,
        "api_version": JUDGE_API_VERSION,
    }
    # A reasoning judge rejects temperature and top_p. The library only leaves them out
    # when told, so a gpt-5 deployment fails every row until this is set.
    options = {"credential": credential, "threshold": EVALUATOR_THRESHOLD,
               "is_reasoning_model": args.judge_reasoning}
    evaluators = {name: cls(config, **options) for name, cls in module["classes"].items()}
    mapping = {
        "groundedness": ["query", "response", "context"],
        "relevance": ["query", "response"],
        "intent_resolution": ["query", "response"],
        "tool_call_accuracy": ["query", "tool_calls", "tool_definitions"],
    }
    evaluator_config = {
        name: {"column_mapping": {column: f"${{data.{column}}}" for column in columns}}
        for name, columns in mapping.items()
    }
    return evaluators, evaluator_config


def score(args, module, level, data_path, credential):
    """Grade one level's rows and publish them.

    Wrapped because a missing role assignment fails here, after every judge call has
    already been paid for. Losing the upload is annoying; losing the scoreboard and the
    rows along with it would mean paying twice to learn the same thing.
    """
    evaluators, evaluator_config = build_evaluators(args, module, credential)
    try:
        return module["evaluate"](
            data=data_path,
            evaluators=evaluators,
            evaluator_config=evaluator_config,
            evaluation_name=f"harness-ladder-{level}",
            azure_ai_project=args.endpoint if args.upload else None,
            credential=credential,
        )
    except Exception as error:  # noqa: BLE001 — grading already cost money, keep the rows
        print(f"  [scoring failed, rows kept at {data_path}] {error}")
        return None


def judge_locally(ctx, records):
    """The fallback rubric. One call per row, no extra packages, no portal.

    Deliberately scores only what a single model can see in one row — groundedness and
    relevance. Faking the other two would put numbers in those columns that mean nothing.
    """
    rubric = ("아래 답변을 근거 문단에 비추어 채점하세요. "
              "groundedness: 답변의 모든 수치가 근거에 실재하면 5, 전혀 없으면 1. "
              "relevance: 질문에 답했으면 5, 빗나갔으면 1.")
    totals = {"groundedness": [], "relevance": []}
    for record in records:
        try:
            response = ctx["client"].responses.parse(
                model=ctx["args"].judge_deployment,
                instructions=rubric,
                input=f"질문: {record['query']}\n\n근거:\n{record['context']}\n\n답변:\n{record['response']}",
                text_format=LocalScore,
            )
        except Exception as error:  # noqa: BLE001 — one unscored row must not void the level
            print(f"  [judge failed for {record['id']}] {error}")
            continue
        totals["groundedness"].append(response.output_parsed.groundedness)
        totals["relevance"].append(response.output_parsed.relevance)
    return {f"{name}.{name}": sum(values) / len(values)
            for name, values in totals.items() if values}


def pick_metric(metrics, evaluator):
    """Read one evaluator's score without hard-coding what the library called it.

    The key is normally "groundedness.groundedness", but the suffix has moved between
    releases and each evaluator also publishes companions like _threshold and
    _binary_aggregate. Matching the prefix and skipping those keeps a working score from
    being reported as n/a — the worst failure here, because it looks like the judge ran
    and found nothing rather than like the board failed to read it.
    """
    exact = metrics.get(f"{evaluator}.{evaluator}")
    if isinstance(exact, (int, float)):
        return exact
    for key, value in metrics.items():
        if key.startswith(f"{evaluator}.") and not key.endswith(("_threshold", "_result")):
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return value
    return None


def summarize(name, records, metrics):
    """One scoreboard row. metrics is whatever the judge produced, or {} if there was none."""
    count = len(records) or 1
    metrics = metrics or {}
    return {
        "level": name,
        "hits": sum(1 for r in records if r["hit"]),
        "total": len(records),
        "groundedness": pick_metric(metrics, "groundedness"),
        "relevance": pick_metric(metrics, "relevance"),
        "intent": pick_metric(metrics, "intent_resolution"),
        "tool_accuracy": pick_metric(metrics, "tool_call_accuracy"),
        "input_tokens": sum(r["input_tokens"] for r in records),
        "output_tokens": sum(r["output_tokens"] for r in records),
        "turns": sum(r["turns"] for r in records) / count,
        "tools": sum(r["tool_call_count"] for r in records) / count,
        "seconds": sum(r["seconds"] for r in records),
        "studio_url": (metrics or {}).get("studio_url"),
    }


def cell(value):
    """n/a is not zero. ToolCallAccuracyEvaluator returns not-applicable for rows with no
    tool calls, and printing that as 0 would read as 'chose the wrong tool' for a level
    that was never given one."""
    return f"{value:6.2f}" if isinstance(value, (int, float)) else "   n/a"


def delta(rows, low, high, field, label, fmt):
    """One before/after line. The table alone leaves people to find the story in it."""
    found = {row["level"]: row for row in rows}
    if low not in found or high not in found:
        return None
    before, after = found[low][field], found[high][field]
    if not isinstance(before, (int, float)) or not isinstance(after, (int, float)):
        return None
    return f"  {low} -> {high}  {label:<12} {fmt(before)} -> {fmt(after)}"


def print_scoreboard(rows, args, document_lines):
    print(f"\n{BANNER}")
    print(f"harness ladder — {rows[0]['total']} rows/level · agent {args.deployment} · "
          f"judge {args.judge_deployment if args.judge != 'none' else 'none'}")
    print(f"{os.path.basename(args.file)} ({document_lines} lines)")
    print(BANNER)
    print(f"{'level':<6}{'hit':>7}{'ground':>8}{'relev':>8}{'intent':>8}{'toolacc':>9}"
          f"{'in_tok':>11}{'out_tok':>10}{'turns':>7}{'tools':>7}{'sec':>8}")
    for row in rows:
        # Built before the f-string rather than inside it. Nesting the same quote
        # character needs Python 3.12, and nothing else in this lab does.
        hit_cell = f"{row['hits']}/{row['total']}"
        print(f"{row['level']:<6}{hit_cell:>7}"
              f"{cell(row['groundedness']):>8}{cell(row['relevance']):>8}"
              f"{cell(row['intent']):>8}{cell(row['tool_accuracy']):>9}"
              f"{row['input_tokens']:>11,}{row['output_tokens']:>10,}"
              f"{row['turns']:>7.1f}{row['tools']:>7.1f}{row['seconds']:>8.1f}")

    print(RULE)
    tokens = lambda v: f"{v:,.0f}"  # noqa: E731 — one-line formatter, a def would say less
    turns = lambda v: f"{v:.1f}"  # noqa: E731
    score_value = lambda v: f"{v:.2f}"  # noqa: E731 — same precision as the table above
    # Hits carry their denominator. "1 -> 6" invites the reader to guess the total.
    hits = lambda v: f"{v:.0f}/{rows[0]['total']}"  # noqa: E731
    for line in (delta(rows, "L0", "L1", "hits", "hit", hits),
                 delta(rows, "L1", "L3", "input_tokens", "in_tok", tokens),
                 delta(rows, "L2", "L3", "turns", "turns", turns),
                 delta(rows, "L2", "L3", "tool_accuracy", "toolacc", score_value),
                 delta(rows, "L3", "L4", "input_tokens", "in_tok", tokens)):
        if line:
            print(line)
    print("\n  L2 and L3 call the same grep over the same file. "
          "Only the tool descriptions differ.")
    print(f"  {rows[0]['total']} rows per level is a demonstration, not a benchmark — "
          f"raise --repeat before believing a small gap.")

    urls = [(row["level"], row["studio_url"]) for row in rows if row.get("studio_url")]
    if urls:
        print("\nFoundry Evaluation:")
        for level, url in urls:
            print(f"  {level}  {url}")


def main():
    args = parse_args()
    document, lines = load_document(args.file)
    if len(document) > MAX_DOCUMENT_CHARS:
        raise SystemExit(f"{args.file} is {len(document):,} characters; L1 sends the whole file "
                         f"in one request and stops fitting past {MAX_DOCUMENT_CHARS:,}")

    golden = resolve_golden(lines, args.questions, args.repeat)
    client = create_client(args)
    ctx = {"client": client, "args": args, "document": document, "lines": lines}

    # api-key and access-token build no credential object, and both the judge and the
    # upload need one. Failing over to the inline judge is better than a stack trace
    # thirty minutes into a run.
    credential = None if args.auth in ("api-key", "access-token") else identity.get_credential(args)
    module = import_evaluation() if args.judge == "evaluation" else None
    if args.judge == "evaluation" and (module is None or credential is None):
        if credential is None:
            print("\n[--auth api-key/access-token cannot sign the judge, falling back to --judge llm]")
        args = argparse.Namespace(**{**vars(args), "judge": "llm"})
        ctx = {**ctx, "args": args}
        module = None

    out_dir = args.out_dir or tempfile.mkdtemp(prefix="harness-ladder-")
    os.makedirs(out_dir, exist_ok=True)

    names = list(LEVELS) if args.level == "all" else [args.level]
    rows = []
    for index, name in enumerate(names, start=1):
        records = run_level(ctx, name, f"[{index}/{len(names)}]", golden)
        path = write_jsonl(records, out_dir, name)
        if args.judge == "evaluation":
            result = score(args, module, name, path, credential)
            metrics = {**(result or {}).get("metrics", {}), "studio_url": (result or {}).get("studio_url")}
        elif args.judge == "llm":
            metrics = judge_locally(ctx, records)
        else:
            metrics = {}
        rows.append(summarize(name, records, metrics))

    print_scoreboard(rows, args, len(lines))
    print(f"\nrows: {out_dir}")


if __name__ == "__main__":
    main()
