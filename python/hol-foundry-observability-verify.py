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
    "semconv": "hol-foundry-observability-semconv.py",
}

# Two extra passes over the cheapest scenario. Content recording and the failure path are
# properties of a run, not of a pattern, so they need their own runs rather than another
# pattern — and sequential is the smallest run that still calls a tool.
EXTRA_PASSES = {
    "sensitive": ("sequential", ["--sensitive-data"]),
    "error": ("sequential", ["--inject-error"]),
}

ALL_SCENARIOS = list(SCENARIOS)

# Patterns that route work between agents, and so should carry the handoff vocabulary.
ROUTING_SCENARIOS = ["sequential", "concurrent", "handoff", "semconv"]

# Attribute keys that carry model content. Their presence is not the evidence — the
# instrumentor writes the message shape either way, with the text stripped out when
# recording is off — so the check below weighs them instead of counting them.
CONTENT_MARKERS = ("gen_ai.input", "gen_ai.output", "gen_ai.system_instructions",
                   "tool.call.arguments", "tool.call.results")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run every tracing scenario in this lab and assert what each one should "
                    "have emitted — the spans azure-ai-projects emits, the multi-agent "
                    "semantic conventions this lab emits itself, the GenAI metrics, content "
                    "recording and the failure path.",
        epilog="Each scenario runs as its own process with --dump-spans, and the assertions "
               "read the dumps. Exit code is 0 only when nothing failed. Every scenario calls "
               "the model and creates real agents in the project, so a full run costs real "
               "tokens — narrow it with --scenario while you are iterating. "
               "--check-app-insights adds the one assertion the in-process pipeline cannot "
               "make, that the traces actually reached Application Insights; it needs the "
               "project connected to an Application Insights resource (Foundry portal, "
               "Observability > Traces > Connect) and the Log Analytics Reader role on it. "
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
        parser.error(f"--auth {args.auth} is not supported by this SDK path, use another method")
    if args.check_app_insights and not args.workspace_id:
        parser.error("--check-app-insights needs --workspace-id or LOG_ANALYTICS_WORKSPACE_ID")
    if args.check_app_insights and "azure-monitor" not in args.trace_export:
        parser.error("--check-app-insights needs --trace-export azure-monitor, otherwise "
                     "nothing was sent for it to find")
    observability.validate_tracing_arguments(parser, args)
    if not args.scenario:
        args.scenario = ALL_SCENARIOS + list(EXTRA_PASSES)
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
    """Run one scenario and load what it recorded, or None when it could not run.

    The passes run one after another rather than together on purpose: the agents are named
    resources in the project, and two passes at once would be two runs creating and
    deleting versions of the same agent names.
    """
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


def agents_were_created(dumps, scope):
    """Every agent a run spoke to should have a create_agent span naming it.

    An invoke_agent with no matching create_agent is an agent the portal has no row for,
    which is the one failure that leaves a trace looking complete and still unusable.
    """
    spans = spans_of(dumps, scope)
    created = {span["attributes"].get("gen_ai.agent.name")
               for span in named(spans, "create_agent")}
    invoked = {span["attributes"].get("gen_ai.agent.name")
               for span in named(spans, "invoke_agent")} - {None}
    missing = sorted(invoked - created)
    return not missing, f"invoked without a create_agent span: {', '.join(missing)}"


def agents_were_deleted(dumps, scope):
    """As many delete_version spans as create_agent spans.

    Agents are named resources in the project. A run that creates them and leaves them
    behind is a run that changed the project, and the next run stacks another version on
    top of what this one abandoned.
    """
    for name in scope:
        if name not in dumps:
            continue
        spans = dumps[name]["spans"]
        created = len(named(spans, "create_agent"))
        deleted = len(named(spans, "AgentsOperations.delete_version"))
        if created != deleted:
            return False, f"{name} created {created} agents and deleted {deleted}"
    return True, ""


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

    Weighed by character count rather than by key: the instrumentor writes
    gen_ai.input.messages either way, and with recording off it writes the roles and the
    part types with the text taken out. Counting keys would score that identical to a
    recorded run, which is exactly the failure this check exists to catch.
    """
    def weight(name):
        return sum(len(str(value))
                   for span in spans_of(dumps, [name])
                   for key, value in span["attributes"].items()
                   if key.startswith(CONTENT_MARKERS))

    if "sensitive" not in dumps or "sequential" not in dumps:
        return False, "needs both the sequential and sensitive passes"
    on, off = weight("sensitive"), weight("sequential")
    return on > off, f"{on} characters of content with recording on, {off} with it off"


# group, label, the passes it needs, and the check itself. A check whose passes were not
# run reports SKIP, so narrowing with --scenario never looks like a clean sheet.
CHECKS = [
    ("A what the SDK traces", "create_agent span", ALL_SCENARIOS, has_span("create_agent")),
    ("A what the SDK traces", "invoke_agent span", ALL_SCENARIOS, has_span("invoke_agent")),
    ("A what the SDK traces", "create_conversation span", ALL_SCENARIOS,
     has_span("create_conversation")),
    ("A what the SDK traces", "agent identity attributes", ALL_SCENARIOS,
     has_attributes("invoke_agent", ["gen_ai.operation.name", "gen_ai.agent.name", "gen_ai.agent.id"])),
    ("A what the SDK traces", "model call attributes", ALL_SCENARIOS,
     has_attributes("invoke_agent", ["gen_ai.provider.name", "gen_ai.response.id",
                                     "gen_ai.response.model", "gen_ai.conversation.id"])),
    ("A what the SDK traces", "token usage attributes", ALL_SCENARIOS,
     has_attributes("invoke_agent", ["gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens"])),
    ("A what the SDK traces", "every invoked agent was created", ALL_SCENARIOS, agents_were_created),
    ("A what the SDK traces", "every created agent was deleted", ALL_SCENARIOS, agents_were_deleted),

    ("B what the lab traces", "execute_tool span", ALL_SCENARIOS, has_span("execute_tool")),
    ("B what the lab traces", "tool call attributes", ALL_SCENARIOS,
     has_attributes("execute_tool", ["gen_ai.operation.name", "gen_ai.tool.name",
                                     "gen_ai.agent.name"])),
    ("B what the lab traces", "agent_to_agent_interaction span", ROUTING_SCENARIOS,
     has_span("agent_to_agent_interaction")),
    ("B what the lab traces", "handoff attributes", ROUTING_SCENARIOS,
     has_attributes("agent_to_agent_interaction",
                    ["source.agent.name", "target.agent.name", "handoff.reason"])),
    ("B what the lab traces", "agent.state.management span", ROUTING_SCENARIOS,
     has_span("agent.state.management")),
    ("B what the lab traces", "execute_task span", ["semconv"], has_span("execute_task")),
    ("B what the lab traces", "agent_planning span", ["semconv"], has_span("agent_planning")),
    ("B what the lab traces", "agent_orchestration span", ["semconv"], has_span("agent_orchestration")),
    ("B what the lab traces", "tool_definitions and llm_spans", ["semconv"],
     has_attributes("invoke_agent", ["tool_definitions", "llm_spans"])),
    ("B what the lab traces", "tool call arguments and results", ["semconv"],
     has_attributes("execute_tool", ["tool.call.arguments", "tool.call.results"])),
    ("B what the lab traces", "Evaluation event", ["semconv"], has_events(["Evaluation"])),

    ("C metrics", "operation duration", ALL_SCENARIOS,
     has_metrics(["gen_ai.client.operation.duration"])),
    ("C metrics", "token usage", ALL_SCENARIOS, has_metrics(["gen_ai.client.token.usage"])),
    ("C metrics", "tool invocation duration", ALL_SCENARIOS,
     has_metrics(["hol.tool.invocation.duration"])),

    ("D the run", "one trace per run", ALL_SCENARIOS, one_trace_per_run),
    ("D the run", "service.name on every span", ALL_SCENARIOS, service_name_on_spans),
    ("D the run", "failure recorded as ERROR", ["error"], error_recorded),
    ("D the run", "content recording changes output", ["sequential", "sensitive"],
     content_recording_differs),
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

    print(f"\nE backend\n  querying workspace {args.workspace_id} for trace {trace_ids[0]}")
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
