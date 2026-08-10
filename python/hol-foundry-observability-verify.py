#!/usr/bin/env python3

import argparse
import json
import os
import subprocess
import sys
import tempfile

import identity
import observability

HERE = os.path.dirname(os.path.abspath(__file__))

# Scenario name to the script that produces it. The scripts are separate processes rather
# than imports because their file names contain dashes and cannot be imported at all —
# and running them as a user would is the only way to prove the scripts themselves work.
SCENARIOS = {
    "sequential": "hol-foundry-observability-sequential.py",
    "concurrent": "hol-foundry-observability-concurrent.py",
    "handoff": "hol-foundry-observability-handoff.py",
    "groupchat": "hol-foundry-observability-groupchat.py",
    "magentic": "hol-foundry-observability-magentic.py",
    "semconv": "hol-foundry-observability-semconv.py",
}

# Two extra passes over the cheapest scenario. Content recording and the failure path are
# properties of a run, not of a pattern, so they need their own runs rather than another
# pattern — and sequential is the smallest run that still calls a tool.
EXTRA_PASSES = {
    "sensitive": ("sequential", ["--sensitive-data"]),
    "error": ("sequential", ["--inject-error"]),
}

BUILDER_SCENARIOS = ["sequential", "concurrent", "handoff", "groupchat", "magentic"]

# Attribute keys that only appear when content recording is on. Checked as a count rather
# than by name: the exact key set moves with the semantic conventions, but "more content
# with it on than off" is what the flag actually promises.
CONTENT_MARKERS = ("gen_ai.input", "gen_ai.output", "gen_ai.prompt", "gen_ai.completion",
                   "gen_ai.request.instructions", "gen_ai.system_instructions",
                   "tool.call.arguments", "tool.call.results")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run every tracing scenario in this lab and assert what each one should "
                    "have emitted — agent spans, workflow spans, the multi-agent semantic "
                    "conventions, GenAI metrics, content recording and the failure path.",
        epilog="Each scenario runs as its own process with --dump-spans, and the assertions "
               "read the dumps. Exit code is 0 only when nothing failed. Every scenario calls "
               "the model, so a full run costs real tokens — narrow it with --scenario while "
               "you are iterating. --check-app-insights adds the one assertion the in-process "
               "pipeline cannot make, that the traces actually reached Application Insights; "
               "it needs the project connected to an Application Insights resource (Foundry "
               "portal, Agents > Traces > Connect) and the Log Analytics Reader role on it. "
               "Traces take 2-5 minutes to land, so that check polls.",
    )
    parser.add_argument("--endpoint",
                        default=os.getenv("FOUNDRY_PROJECT_ENDPOINT") or os.getenv("AZURE_AI_PROJECT_ENDPOINT"),
                        help="Foundry project endpoint")
    parser.add_argument("--deployment",
                        default=os.getenv("FOUNDRY_MODEL_NAME")
                        or os.getenv("AZURE_AI_MODEL_DEPLOYMENT_NAME", "gpt-5.6-terra"),
                        help="model deployment name")

    identity.add_auth_arguments(parser)

    observability.add_tracing_arguments(parser)

    group = parser.add_argument_group("what to check")
    group.add_argument("--scenario", action="append", default=[],
                       choices=sorted(SCENARIOS) + sorted(EXTRA_PASSES),
                       help="run only these, repeat for several. Everything by default")
    group.add_argument("--check-app-insights", action="store_true",
                       help="also assert the traces reached Application Insights")
    group.add_argument("--workspace-id",
                       default=os.getenv("LOG_ANALYTICS_WORKSPACE_ID"),
                       help="Log Analytics workspace GUID behind the Application Insights "
                            "resource, or LOG_ANALYTICS_WORKSPACE_ID")
    group.add_argument("--keep-dumps", metavar="DIR",
                       help="write the span dumps here instead of a temporary directory")

    args = parser.parse_args()
    if not args.endpoint:
        parser.error("--endpoint or FOUNDRY_PROJECT_ENDPOINT is required")
    if args.auth in ("api-key", "access-token"):
        parser.error(f"--auth {args.auth} is not supported by the agent framework, use another method")
    if args.check_app_insights and not args.workspace_id:
        parser.error("--check-app-insights needs --workspace-id or LOG_ANALYTICS_WORKSPACE_ID")
    if args.check_app_insights and "azure-monitor" not in args.trace_export:
        parser.error("--check-app-insights needs --trace-export azure-monitor, otherwise "
                     "nothing was sent for it to find")
    observability.validate_tracing_arguments(parser, args)
    if not args.scenario:
        args.scenario = list(SCENARIOS) + list(EXTRA_PASSES)
    return args


def build_command(args, script, dump_path, extra):
    """The command line a reader could have typed. Only the flags that change behaviour are
    forwarded — anything else would make a failure here hard to reproduce by hand."""
    command = [sys.executable, os.path.join(HERE, script),
               "--endpoint", args.endpoint,
               "--deployment", args.deployment,
               "--auth", args.auth,
               "--service-name", args.service_name,
               "--sampling-ratio", str(args.sampling_ratio),
               "--dump-spans", dump_path]
    for export in args.trace_export:
        command += ["--trace-export", export]
    if args.connection_string:
        command += ["--connection-string", args.connection_string]
    return command + extra


def run_pass(args, name, dump_dir):
    """Run one scenario and load what it recorded, or None when it could not run."""
    scenario, extra = EXTRA_PASSES.get(name, (name, []))
    dump_path = os.path.join(dump_dir, f"{name}.json")
    command = build_command(args, SCENARIOS[scenario], dump_path, extra)

    print(f"\n=== {name} ===")
    result = subprocess.run(command, cwd=HERE, check=False)
    if result.returncode != 0:
        print(f"  scenario exited {result.returncode}, nothing to assert")
        return None
    if not os.path.isfile(dump_path):
        print(f"  scenario wrote no dump at {dump_path}")
        return None

    with open(dump_path, encoding="utf-8") as handle:
        return json.load(handle)


def spans_of(dumps, names=None):
    """Every span from the named passes, or from all of them."""
    selected = dumps if names is None else {k: v for k, v in dumps.items() if k in names}
    return [span for dump in selected.values() for span in dump["spans"]]


def named(spans, prefix):
    return [span for span in spans if span["name"] == prefix or span["name"].startswith(f"{prefix} ")]


def attribute_keys(spans):
    return {key for span in spans for key in span["attributes"]}


def event_names(spans):
    return {event["name"] for span in spans for event in span["events"]}


def has_span(prefix):
    return lambda dumps, scope: (
        bool(named(spans_of(dumps, scope), prefix)),
        f"no span named {prefix}",
    )


def has_attributes(prefix, keys):
    """A missing attribute and a missing span read the same in a matrix unless they are
    told apart — and the fix for each is nothing alike."""
    def check(dumps, scope):
        spans = named(spans_of(dumps, scope), prefix)
        if not spans:
            return False, f"there were no {prefix} spans to carry {', '.join(sorted(keys))}"
        missing = sorted(set(keys) - attribute_keys(spans))
        return not missing, f"{prefix} spans are missing {', '.join(missing)}"
    return check


def has_events(names):
    def check(dumps, scope):
        missing = sorted(set(names) - event_names(spans_of(dumps, scope)))
        return not missing, f"no span carried the events {', '.join(missing)}"
    return check


def has_metrics(names):
    def check(dumps, scope):
        seen = {name for key in scope if key in dumps for name in dumps[key]["metrics"]}
        missing = sorted(set(names) - seen)
        return not missing, f"never recorded {', '.join(missing)}"
    return check


def has_links(dumps, scope):
    linked = [span for span in spans_of(dumps, scope) if span["links"]]
    return bool(linked), "no span was linked to the message.send that caused it"


def buffered_delivery(dumps, scope):
    statuses = {span["attributes"].get("edge_group.delivery_status")
                for span in named(spans_of(dumps, scope), "edge_group.process")}
    return "buffered" in statuses, f"fan-in never buffered, saw {sorted(s for s in statuses if s)}"


def one_trace_per_run(dumps, scope):
    for name in scope:
        if name not in dumps:
            continue
        ids = {span["trace_id"] for span in dumps[name]["spans"]}
        if len(ids) > 1:
            return False, f"{name} produced {len(ids)} traces, the run should be one"
    return True, ""


def service_name_on_spans(dumps, scope):
    for name in scope:
        if name not in dumps:
            continue
        expected = dumps[name]["service_name"]
        actual = {span["resource"].get("service.name") for span in dumps[name]["spans"]}
        if actual != {expected}:
            return False, f"{name} spans carry service.name {sorted(a for a in actual if a)}, expected {expected}"
    return True, ""


def error_recorded(dumps, scope):
    failed = [span for span in spans_of(dumps, scope) if span["status"] == "ERROR"]
    if not failed:
        return False, "no span ended in ERROR — the model may have skipped the failing tool call"
    typed = [span for span in failed if "error.type" in span["attributes"]]
    return bool(typed), f"{len(failed)} ERROR spans but none carried error.type"


def content_recording_differs(dumps, scope):
    """Content recording is a switch, so the evidence is a difference, not a presence.

    Counting marker attributes rather than naming one is deliberate: which key holds the
    prompt moves with the semantic conventions, but the flag's promise — more user content
    with it on than off — does not.
    """
    def markers(name):
        return sum(1 for key in attribute_keys(spans_of(dumps, [name]))
                   if key.startswith(CONTENT_MARKERS))

    if "sensitive" not in dumps or "sequential" not in dumps:
        return False, "needs both the sequential and sensitive passes"
    on, off = markers("sensitive"), markers("sequential")
    return on > off, f"{on} content attributes with recording on, {off} with it off"


# group, label, the passes it needs, and the check itself. A check whose passes were not
# run reports SKIP, so narrowing with --scenario never looks like a clean sheet.
CHECKS = [
    ("A agent", "invoke_agent span", BUILDER_SCENARIOS, has_span("invoke_agent")),
    ("A agent", "chat span", BUILDER_SCENARIOS, has_span("chat")),
    ("A agent", "execute_tool span", BUILDER_SCENARIOS, has_span("execute_tool")),
    ("A agent", "create_agent span", BUILDER_SCENARIOS, has_span("create_agent")),
    ("A agent", "agent identity attributes", BUILDER_SCENARIOS,
     has_attributes("invoke_agent", ["gen_ai.operation.name", "gen_ai.agent.name", "gen_ai.agent.id"])),
    ("A agent", "model call attributes", BUILDER_SCENARIOS,
     has_attributes("chat", ["gen_ai.system", "gen_ai.response.id"])),
    ("A agent", "token usage attributes", BUILDER_SCENARIOS,
     has_attributes("chat", ["gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens"])),

    ("B workflow", "workflow.build span", BUILDER_SCENARIOS, has_span("workflow.build")),
    ("B workflow", "workflow.run span", BUILDER_SCENARIOS, has_span("workflow.run")),
    ("B workflow", "executor.process span", BUILDER_SCENARIOS, has_span("executor.process")),
    ("B workflow", "edge_group.process span", BUILDER_SCENARIOS, has_span("edge_group.process")),
    ("B workflow", "message.send span", BUILDER_SCENARIOS, has_span("message.send")),
    ("B workflow", "workflow.build attributes", BUILDER_SCENARIOS,
     has_attributes("workflow.build", ["workflow.id", "workflow.definition", "workflow_builder.name"])),
    ("B workflow", "workflow.run attributes", BUILDER_SCENARIOS,
     has_attributes("workflow.run", ["workflow.id", "workflow.name"])),
    ("B workflow", "executor attributes", BUILDER_SCENARIOS,
     has_attributes("executor.process", ["executor.id", "executor.type", "message.type"])),
    ("B workflow", "edge group attributes", BUILDER_SCENARIOS,
     has_attributes("edge_group.process",
                    ["edge_group.type", "edge_group.id", "edge_group.delivered",
                     "edge_group.delivery_status"])),
    ("B workflow", "build lifecycle events", BUILDER_SCENARIOS,
     has_events(["build.started", "build.validation_completed", "build.completed"])),
    ("B workflow", "run lifecycle events", BUILDER_SCENARIOS,
     has_events(["workflow.started", "workflow.completed"])),
    ("B workflow", "causal span links", BUILDER_SCENARIOS, has_links),
    ("B workflow", "fan-in buffers messages", ["concurrent"], buffered_delivery),

    ("C semconv", "execute_task span", ["semconv"], has_span("execute_task")),
    ("C semconv", "agent_planning span", ["semconv"], has_span("agent_planning")),
    ("C semconv", "agent_orchestration span", ["semconv"], has_span("agent_orchestration")),
    ("C semconv", "agent_to_agent_interaction span", ["semconv"], has_span("agent_to_agent_interaction")),
    ("C semconv", "agent.state.management span", ["semconv"], has_span("agent.state.management")),
    ("C semconv", "tool_definitions and llm_spans", ["semconv"],
     has_attributes("invoke_agent", ["tool_definitions", "llm_spans"])),
    ("C semconv", "tool call arguments and results", ["semconv"],
     has_attributes("execute_tool", ["tool.call.arguments", "tool.call.results"])),
    ("C semconv", "Evaluation event", ["semconv"], has_events(["Evaluation"])),

    ("D metrics", "operation duration", BUILDER_SCENARIOS,
     has_metrics(["gen_ai.client.operation.duration"])),
    ("D metrics", "token usage", BUILDER_SCENARIOS, has_metrics(["gen_ai.client.token.usage"])),
    ("D metrics", "function invocation duration", BUILDER_SCENARIOS,
     has_metrics(["agent_framework.function.invocation.duration"])),

    ("E run", "one trace per run", list(SCENARIOS), one_trace_per_run),
    ("E run", "service.name on every span", list(SCENARIOS), service_name_on_spans),
    ("E run", "failure recorded as ERROR", ["error"], error_recorded),
    ("E run", "content recording changes output", ["sequential", "sensitive"], content_recording_differs),
]


def report(dumps, ran):
    """Print the matrix and say how many of each outcome there were."""
    counts = {"PASS": 0, "FAIL": 0, "SKIP": 0}
    group = None

    for check_group, label, scope, check in CHECKS:
        if check_group != group:
            group = check_group
            print(f"\n{group}")

        available = [name for name in scope if name in dumps]
        if not available:
            missing = [name for name in scope if name not in ran]
            counts["SKIP"] += 1
            print(f"  SKIP {label} — needs {', '.join(missing) or 'a pass that did not finish'}")
            continue

        ok, detail = check(dumps, available)
        counts["PASS" if ok else "FAIL"] += 1
        print(f"  {'PASS' if ok else 'FAIL'} {label}{'' if ok else f' — {detail}'}")

    return counts


def check_app_insights(args, dumps):
    """The only assertion that leaves the process. Traces are per run, so one trace id is
    enough — if the pipeline delivered one it is configured, and if it delivered none the
    in-process passes above already proved the spans existed."""
    trace_ids = [dump["spans"][0]["trace_id"] for dump in dumps.values() if dump["spans"]]
    if not trace_ids:
        print("  FAIL app insights — no run produced a trace id to look for")
        return False

    print(f"\nF backend\n  querying workspace {args.workspace_id} for trace {trace_ids[0]}")
    found = observability.query_app_insights(args.workspace_id, trace_ids[0],
                                             identity.get_credential(args))
    print(f"  {'PASS' if found else 'FAIL'} traces reached Application Insights"
          f"{'' if found else ' — nothing matched, check the connection string and the role assignment'}")
    return bool(found)


def run_all(args, dump_dir):
    dumps = {}
    for name in args.scenario:
        dump = run_pass(args, name, dump_dir)
        if dump is not None:
            dumps[name] = dump
    return dumps


def main():
    args = parse_args()

    if args.keep_dumps:
        os.makedirs(args.keep_dumps, exist_ok=True)
        dumps = run_all(args, args.keep_dumps)
    else:
        with tempfile.TemporaryDirectory(prefix="hol-obs-") as dump_dir:
            dumps = run_all(args, dump_dir)

    if not dumps:
        raise SystemExit("no scenario produced a span dump, so there is nothing to assert")

    print(f"\n{'=' * 60}\nfeature matrix over {len(dumps)} of {len(args.scenario)} passes")
    counts = report(dumps, set(dumps))

    backend_ok = True
    if args.check_app_insights:
        backend_ok = check_app_insights(args, dumps)

    print(f"\n{counts['PASS']} pass, {counts['FAIL']} fail, {counts['SKIP']} skip")
    if counts["FAIL"] or not backend_ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
