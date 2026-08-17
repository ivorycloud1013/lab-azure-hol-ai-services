#!/usr/bin/env python3
"""Step 5 — the evaluation layer. The thing that tells you the last change was a mistake.

Every step so far printed numbers for one run. That is not evaluation, it is a readout.
Evaluation is the part that keeps yesterday's run and tells you, by name, which question
stopped working today — because a harness change that fixes one case and quietly breaks
two is the normal outcome, not the unlucky one.

    python step5_eval.py --endpoint ... --out runs/a.json     # record
    # change something in step1_tools.py — a description, an error string
    python step5_eval.py --endpoint ... --baseline runs/a.json # compare

The default judge is none, and most of the report still fills in: hit, tokens, rounds and
tool errors are all deterministic. --judge evaluation adds azure-ai-evaluation's graded
scores and publishes the run to the Foundry Evaluation tab, which costs real money per
row and is why it is not the default.
"""

import json
import os
import tempfile
import time

import harness_cli
import harness_loop
import harness_metrics as metrics
import step1_tools
from golden import is_hit

INSTRUCTIONS = step1_tools.INSTRUCTIONS

# azure-ai-evaluation defaults to 2024-02-15-preview, which recent deployments reject.
JUDGE_API_VERSION = "2024-12-01-preview"
EVALUATOR_THRESHOLD = 3


def account_endpoint(url):
    """Strip the project path off, leaving the account root.

    One --endpoint serves two callers that disagree: evaluate() uploads to the project,
    the evaluators reach their judge over the classic AzureOpenAI path which wants the
    account. Deriving one from the other keeps this to a single argument.
    """
    marker = "/api/projects/"
    base = url.split(marker)[0] if marker in url else url
    return base.rstrip("/")


def answer_all(ctx):
    """Run the full harness over the question set, one record per question."""
    records = []
    for item in ctx["golden"]:
        before = len(ctx["run"]["tool_calls"])
        started = time.perf_counter()
        text, _ = harness_loop.run_turn(ctx, item["question"], step1_tools.TOOLS,
                                        step1_tools.dispatch, INSTRUCTIONS)
        calls = ctx["run"]["tool_calls"][before:]
        hit = is_hit(item, text)
        print(f"  {'hit ' if hit else 'miss'} {item['id']}  {len(calls)} calls")
        records.append({
            "id": item["id"],
            "query": item["question"],
            "response": text,
            "context": item["context"],
            "ground_truth": " / ".join(item["answer_key"]),
            "tool_calls": [{"type": "tool_call", "name": c["name"], "arguments": c["arguments"]}
                           for c in calls],
            "tool_definitions": step1_tools.TOOLS,
            "hit": hit,
            "calls": len(calls),
        })
    return records


def compare(records, baseline_path):
    """Name what changed, question by question.

    A pass rate that went from 5/6 to 5/6 can still be two regressions and two fixes.
    Reporting only the aggregate is how a harness rots while its dashboard stays green.
    """
    try:
        with open(baseline_path, encoding="utf-8") as handle:
            baseline = json.load(handle)
    except (OSError, ValueError) as error:
        raise SystemExit(f"could not read baseline {baseline_path}: {error}")

    was = {row["id"]: row for row in baseline.get("records", [])}
    regressions, fixes, drift = [], [], []
    for record in records:
        old = was.get(record["id"])
        if old is None:
            continue
        if old["hit"] and not record["hit"]:
            regressions.append(record["id"])
        elif not old["hit"] and record["hit"]:
            fixes.append(record["id"])
        if record["calls"] > old["calls"]:
            drift.append(f"{record['id']} {old['calls']}->{record['calls']} calls")
    return regressions, fixes, drift


def import_evaluation():
    """Import azure-ai-evaluation, or return None and keep going without it.

    By the time this runs the deterministic half of the report is already recorded, and
    that half carries the regression check. A version skew in a transitive dependency
    should cost the graded scores, not the run.
    """
    try:
        from azure.ai.evaluation import (  # noqa: PLC0415
            GroundednessEvaluator,
            IntentResolutionEvaluator,
            RelevanceEvaluator,
            ToolCallAccuracyEvaluator,
            evaluate,
        )
    except Exception as error:  # noqa: BLE001 — any import failure downgrades the judge
        print(f"  [azure-ai-evaluation unavailable, skipping graded scores: {error}]")
        return None
    return {"evaluate": evaluate, "classes": {
        "groundedness": GroundednessEvaluator,
        "relevance": RelevanceEvaluator,
        "intent_resolution": IntentResolutionEvaluator,
        "tool_call_accuracy": ToolCallAccuracyEvaluator,
    }}


def pick_metric(results, evaluator):
    """Read one evaluator's score without hard-coding what the library called it.

    The key is normally "groundedness.groundedness", but the suffix has moved between
    releases and each evaluator publishes companions like _threshold. Matching the prefix
    keeps a working score from being reported as n/a, which is the worst failure here —
    it looks like the judge ran and found nothing.
    """
    exact = results.get(f"{evaluator}.{evaluator}")
    if isinstance(exact, (int, float)) and not isinstance(exact, bool):
        return exact
    for key, value in results.items():
        if key.startswith(f"{evaluator}.") and not key.endswith(("_threshold", "_result")):
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return value
    return None


def grade(args, records, out_dir, credential):
    """Score the rows with azure-ai-evaluation and publish them.

    evaluate() reads from a path, not from memory, so the rows go to disk first. The whole
    call is wrapped because a missing role assignment fails after every judge call has
    been paid for, and losing the report along with the upload would mean paying twice.
    """
    module = import_evaluation()
    if module is None or credential is None:
        return {}

    path = os.path.join(out_dir, "rows.jsonl")
    with open(path, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    config = {"azure_endpoint": account_endpoint(args.endpoint),
              "azure_deployment": args.judge_deployment,
              "api_version": JUDGE_API_VERSION}
    # A reasoning judge rejects temperature and top_p, and the library only leaves them
    # out when told. Without this a gpt-5 judge fails every row.
    options = {"credential": credential, "threshold": EVALUATOR_THRESHOLD,
               "is_reasoning_model": args.judge_reasoning}
    evaluators = {name: cls(config, **options) for name, cls in module["classes"].items()}
    mapping = {"groundedness": ["query", "response", "context"],
               "relevance": ["query", "response"],
               "intent_resolution": ["query", "response"],
               "tool_call_accuracy": ["query", "tool_calls", "tool_definitions"]}
    evaluator_config = {
        name: {"column_mapping": {column: f"${{data.{column}}}" for column in columns}}
        for name, columns in mapping.items()}

    try:
        result = module["evaluate"](
            data=path, evaluators=evaluators, evaluator_config=evaluator_config,
            evaluation_name="harness-step5",
            azure_ai_project=args.endpoint if args.upload else None,
            credential=credential)
    except Exception as error:  # noqa: BLE001 — grading already cost money, keep the rows
        print(f"  [grading failed, rows kept at {path}] {error}")
        return {}

    scores = {name: pick_metric(result.get("metrics", {}), name) for name in mapping}
    if result.get("studio_url"):
        scores["studio_url"] = result["studio_url"]
    return scores


def parse_args():
    parser = harness_cli.build_parser(
        description="Step 5 — build the evaluation layer: record a run, then catch regressions.",
        epilog="--judge none (the default) still reports hit, tokens, rounds and tool errors. "
               "--judge evaluation adds graded scores and costs money per row.",
    )
    parser.add_argument("--out", default=None, metavar="JSON",
                        help="write this run so a later run can compare against it")
    parser.add_argument("--baseline", default=None, metavar="JSON",
                        help="an earlier --out file; report what got worse since")
    parser.add_argument("--judge", choices=["none", "evaluation"], default="none")
    parser.add_argument("--judge-deployment", default="gpt-4.1", help="model that grades")
    parser.add_argument("--judge-reasoning", action="store_true",
                        help="the judge deployment is a reasoning model")
    parser.add_argument("--no-upload", dest="upload", action="store_false",
                        help="grade locally without publishing to the Foundry Evaluation tab")
    args = harness_cli.finish_parsing(parser)
    if args.judge == "evaluation" and args.upload and "/api/projects/" not in args.endpoint:
        parser.error("--endpoint must be a project endpoint (.../api/projects/<name>) to "
                     "upload, or pass --no-upload")
    return args


def main():
    args = parse_args()
    ctx = {**harness_cli.prepare(args), "run": metrics.new_run()}

    metrics.header("step 5 — evaluation",
                   f"{args.deployment} · {len(ctx['golden'])} questions · judge {args.judge}")

    started = time.perf_counter()
    records = answer_all(ctx)
    elapsed = time.perf_counter() - started
    hits = sum(1 for record in records if record["hit"])

    extra = {}
    if args.judge == "evaluation":
        # api-key and access-token build no credential object, and the judge needs one.
        credential = None
        if args.auth not in ("api-key", "access-token"):
            import identity  # noqa: PLC0415 — only this branch needs it
            credential = identity.get_credential(args)
        elif args.judge == "evaluation":
            print("  [--auth api-key/access-token cannot sign the judge, skipping grades]")
        out_dir = tempfile.mkdtemp(prefix="harness-eval-")
        for name, value in grade(args, records, out_dir, credential).items():
            extra[name] = f"{value:.2f}" if isinstance(value, float) else value

    metrics.report("evaluation", ctx["run"], elapsed, hits, len(records), extra=extra)

    if args.baseline:
        regressions, fixes, drift = compare(records, args.baseline)
        print(f"\n  vs {args.baseline}")
        print(f"    regressions  {', '.join(regressions) if regressions else 'none'}")
        print(f"    fixes        {', '.join(fixes) if fixes else 'none'}")
        print(f"    more calls   {', '.join(drift) if drift else 'none'}")
        if regressions:
            print("\n  A regression is a question that used to pass. Fix it or accept it")
            print("  on purpose — but do not find out about it from a user.")

    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump({"deployment": args.deployment, "records": records}, handle,
                      ensure_ascii=False, indent=2)
        print(f"\n  recorded: {args.out}")
        print("  Change something in step1_tools.py, run again with --baseline, and see")
        print("  whether the change you were sure about actually helped.")


if __name__ == "__main__":
    main()
