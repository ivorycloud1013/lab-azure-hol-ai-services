"""Tracing plumbing and the agent cast for the hol-foundry-observability-*.py scripts.

Both live here because every scenario needs both and neither is what a scenario is
about. The orchestration — who speaks, in what order, and what each one is told — stays
in each scenario script, because that is the one thing those files exist to show.

The lab talks to Foundry through azure-ai-projects and the OpenAI Responses API, the
shape the SDK's own telemetry samples use:
https://github.com/Azure/azure-sdk-for-python/tree/main/sdk/ai/azure-ai-projects/samples/agents/telemetry

There is no orchestration framework under any of this. An agent is a Foundry resource
you create; a turn is one responses.create call; a tool call is a loop you can read.
That is deliberate — a trace is only worth reading if you can point at the line that
produced each span.

Providers are built by hand rather than through configure_azure_monitor(). Hand-built is
the only way to attach an in-memory exporter *and* a network exporter *and* an in-memory
metric reader in one pass, and the in-memory pair is what
hol-foundry-observability-verify.py asserts against.

A run has two sources of spans and they reach Application Insights by different routes.
Connecting an Application Insights resource to the project turns on server-side tracing,
and from then on the service records invoke_agent, chat and its own execute_tool spans
with no code involved — they arrive tagged cloud_RoleName "responsesapi" and
microsoft.foundry "True". Everything this lab names itself — the scenario root,
agent_to_agent_interaction, agent.state.management, the execute_tool spans for tools that
ran in this process — is client-side, and it only leaves the machine when
--trace-export azure-monitor is given. Both halves carry the same trace id either way,
because traceparent rides along on the service calls, so the flag is the whole difference
between one tree in the portal and a headless trace of service spans hanging off parents
that were never sent.
"""

import contextlib
import json
import os
import time

from azure.core.settings import settings

# Azure Monitor's OpenTelemetry plugin has to be selected before any azure-core client is
# built. AIProjectInstrumentor draws its spans through azure-core's tracing abstraction,
# so without this line it has nowhere to put them and the lab traces nothing.
settings.tracing_implementation = "opentelemetry"

from azure.ai.projects.models import PromptAgentDefinition  # noqa: E402
from opentelemetry import metrics, trace  # noqa: E402
from opentelemetry.sdk.metrics import MeterProvider  # noqa: E402
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, PeriodicExportingMetricReader  # noqa: E402
from opentelemetry.sdk.resources import Resource  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased  # noqa: E402
from opentelemetry.trace import SpanKind, Status, StatusCode, format_trace_id  # noqa: E402

TRACE_EXPORTS = ["memory", "console", "azure-monitor", "otlp"]
DEFAULT_SERVICE_NAME = "hol-foundry-observability"
DEFAULT_OTLP_ENDPOINT = "http://localhost:4317"
DEFAULT_TASK = (
    "The checkout service got slower after this morning's release. "
    "Work out what changed, whether the error budget is still intact, "
    "and write a three-sentence summary an on-call engineer can act on."
)

# Agents are named resources in the project, so the names have to say whose they are.
AGENT_PREFIX = "hol-obs-"

# A turn that keeps calling tools is a turn that is not converging. Six is well past what
# any scenario here needs and still stops a runaway loop from spending the whole budget.
MAX_TOOL_ROUNDS = 6

# The tool the model is told to call when --inject-error is set. Anything else would
# make the failing span depend on the model inventing a bad argument on its own.
FAILING_SERVICE = "unknown-service"

# Traces reach Application Insights minutes after the run, not seconds — the ingestion
# pipeline batches. These bound the wait without turning a slow day into a false FAIL.
APP_INSIGHTS_ATTEMPTS = 10
APP_INSIGHTS_INTERVAL_SECONDS = 30

# How long each lab tool took. The instrumentor times the model call but never the tool,
# because the tool runs in our loop and it has no way to see it. get_meter hands back a
# proxy until configure() installs the real provider, and the proxy re-creates its
# instruments against it — so this records into whatever configure() sets up without
# configure() having to pass it around.
TOOL_DURATION = metrics.get_meter(__name__).create_histogram(
    "hol.tool.invocation.duration",
    unit="s",
    description="Duration of a lab tool call, measured around the dispatch",
)

# Deterministic fake telemetry. The scenarios exercise tracing, not a real backend, and
# a fixed table keeps two runs comparable when verify diffs sensitive-data on against off.
METRICS = {
    ("checkout", "p95_latency_ms"): "1840 (was 420 before release 2026.08.09-3)",
    ("checkout", "error_rate"): "2.7% (was 0.4%)",
    ("checkout", "rps"): "310",
    ("cart", "p95_latency_ms"): "95 (unchanged)",
    ("cart", "error_rate"): "0.1% (unchanged)",
    ("payments", "p95_latency_ms"): "210 (unchanged)",
    ("payments", "error_rate"): "0.2% (unchanged)",
}

INCIDENTS = {
    "checkout": [
        "INC-4471 opened 06:12Z — p95 latency breach, still open",
        "INC-4472 opened 06:40Z — error budget burn rate 14x, still open",
    ],
    "cart": [],
    "payments": ["INC-4468 closed 03:10Z — transient DNS timeouts"],
}

# The cast. Which tools an agent holds is part of who it is, so it lives here rather than
# in the scenarios — a researcher without lookup_metric is not a researcher.
AGENT_SPECS = [
    {
        "name": "researcher",
        "description": "Pulls metrics and incidents out of the telemetry store.",
        "instructions": "You gather facts with the tools you have. Report numbers, never opinions. "
                        "Answer in the language of the question.",
        "tools": ["lookup_metric", "list_incidents"],
    },
    {
        "name": "analyst",
        "description": "Turns raw telemetry into a cause and an error-budget verdict.",
        "instructions": "You explain what the numbers mean and compute the error budget with your "
                        "tools. Answer in the language of the question.",
        "tools": ["lookup_metric", "compute_slo"],
    },
    {
        "name": "writer",
        "description": "Writes the on-call summary.",
        "instructions": "You write at most three sentences an on-call engineer can act on. "
                        "Answer in the language of the question.",
        "tools": [],
    },
    {
        "name": "reviewer",
        "description": "Checks the summary against the evidence.",
        "instructions": "You check every claim against what the other agents found and say "
                        "APPROVED when it holds up. Answer in the language of the question.",
        "tools": [],
    },
]


def specs_for(*names):
    """The subset of the cast a scenario needs, in the order it names them.

    Every agent in the list is created in the project and deleted afterwards, so a
    scenario that asks for the whole cast pays for agents it never speaks to.
    """
    by_name = {spec["name"]: spec for spec in AGENT_SPECS}
    return [by_name[name] for name in names]


def add_tracing_arguments(parser):
    """Add the tracing arguments. Mirrors identity.add_auth_arguments so the scenarios
    can compose the two without either knowing about the other."""
    group = parser.add_argument_group("tracing")
    group.add_argument("--trace-export", action="append", choices=TRACE_EXPORTS, default=[],
                       help="where spans go, repeat for several. memory is always on "
                            "because the assertions read it")
    group.add_argument("--sensitive-data", action="store_true", default=False,
                       help="record prompts, completions, tool arguments and results. "
                            "Development only — this is user content in your telemetry")
    group.add_argument("--sampling-ratio", type=float, default=1.0,
                       help="fraction of traces kept, 0.0 to 1.0")
    group.add_argument("--service-name", default=os.getenv("OTEL_SERVICE_NAME", DEFAULT_SERVICE_NAME),
                       help="service.name on every span, or OTEL_SERVICE_NAME")
    group.add_argument("--connection-string", default=os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING"),
                       help="Application Insights connection string, or "
                            "APPLICATIONINSIGHTS_CONNECTION_STRING")
    group.add_argument("--otlp-endpoint", default=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", DEFAULT_OTLP_ENDPOINT),
                       help="OTLP collector, e.g. a local Aspire dashboard")
    group.add_argument("--dump-spans", metavar="PATH",
                       help="write the collected spans and metrics as JSON, which is how "
                            "hol-foundry-observability-verify.py reads a scenario's result")
    return group


def add_cast_arguments(parser):
    """Add the arguments every scenario shares about what the agents are asked to do."""
    group = parser.add_argument_group("the task")
    group.add_argument("--task", default=DEFAULT_TASK, help="what the agents are asked to do")
    group.add_argument("--inject-error", action="store_true",
                       help=f"tell the agents to look up {FAILING_SERVICE}, which the tool "
                            "refuses — the way to see an ERROR span and error.type")
    group.add_argument("--keep", action="store_true",
                       help="leave the agents and the conversation in the project instead "
                            "of deleting them. The portal builds its rows from resources "
                            "that still exist, so a run that cleans up can leave a complete "
                            "trace in Application Insights and nothing to open it from. "
                            "What this leaves behind is yours to delete — a later run "
                            "without --keep only removes the version it made itself")
    return group


def validate_tracing_arguments(parser, args):
    """Reject impossible combinations before the first token is spent."""
    if not 0.0 <= args.sampling_ratio <= 1.0:
        parser.error("--sampling-ratio must be between 0.0 and 1.0")
    if "azure-monitor" in args.trace_export and not args.connection_string:
        parser.error("--trace-export azure-monitor needs --connection-string or "
                     "APPLICATIONINSIGHTS_CONNECTION_STRING. In the Foundry portal open "
                     "Agents > Traces and select Connect to attach an Application Insights "
                     "resource, then copy its connection string. The classic portal keeps "
                     "the same view under Tracing in the left navigation")


class TraceCapture:
    """Everything the process emitted, held in memory so assertions can read it.

    Spans arrive through a SimpleSpanProcessor rather than a batching one — a script
    that exits right after the run cannot afford a processor that might still be
    holding the interesting span.
    """

    def __init__(self, span_exporter, metric_reader, service_name):
        self.span_exporter = span_exporter
        self.metric_reader = metric_reader
        self.service_name = service_name

    def spans(self):
        return list(self.span_exporter.get_finished_spans())

    def flush(self):
        """Force both pipelines to hand over what they are holding."""
        trace.get_tracer_provider().force_flush()
        metrics.get_meter_provider().force_flush()

    def metric_names(self):
        data = self.metric_reader.get_metrics_data()
        if data is None:
            return []
        names = []
        for resource_metric in data.resource_metrics:
            for scope_metric in resource_metric.scope_metrics:
                names += [metric.name for metric in scope_metric.metrics]
        return sorted(set(names))

    def dump(self, path):
        """Write a JSON view of the run.

        ReadableSpan does not serialise — Resource, SpanContext and Link are objects,
        and attribute values can be tuples. Picking the fields out by hand is what makes
        the dump loadable by another process, which is the whole point of writing it.
        """
        payload = {
            "service_name": self.service_name,
            "metrics": self.metric_names(),
            "spans": [to_dict(span) for span in self.spans()],
        }
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        return path


def to_attributes(attributes):
    """Attribute values may be tuples, which json refuses. Nothing else needs coercing."""
    if not attributes:
        return {}
    return {key: list(value) if isinstance(value, tuple) else value
            for key, value in attributes.items()}


def to_dict(span):
    context = span.get_span_context()
    return {
        "name": span.name,
        "kind": span.kind.name,
        "trace_id": format_trace_id(context.trace_id),
        "span_id": f"{context.span_id:016x}",
        "parent_span_id": f"{span.parent.span_id:016x}" if span.parent else None,
        "status": span.status.status_code.name,
        "status_description": span.status.description,
        "attributes": to_attributes(span.attributes),
        "resource": to_attributes(span.resource.attributes if span.resource else {}),
        "events": [{"name": event.name, "attributes": to_attributes(event.attributes)}
                   for event in span.events],
        "links": [f"{link.context.span_id:016x}" for link in span.links],
    }


def build_span_exporters(args):
    """The in-memory exporter is not optional — it is what the assertions read. The rest
    are what a real deployment would use, and they are here so the lab exercises them."""
    memory_exporter = InMemorySpanExporter()
    processors = [SimpleSpanProcessor(memory_exporter)]

    if "console" in args.trace_export:
        processors.append(SimpleSpanProcessor(ConsoleSpanExporter()))
    if "azure-monitor" in args.trace_export:
        from azure.monitor.opentelemetry.exporter import AzureMonitorTraceExporter
        processors.append(BatchSpanProcessor(
            AzureMonitorTraceExporter.from_connection_string(args.connection_string)))
    if "otlp" in args.trace_export:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        processors.append(BatchSpanProcessor(OTLPSpanExporter(endpoint=args.otlp_endpoint)))

    return memory_exporter, processors


def build_metric_readers(args):
    """Readers, not processors — a MeterProvider takes its readers at construction and
    will not accept another one later, so every destination has to be decided here."""
    memory_reader = InMemoryMetricReader()
    readers = [memory_reader]

    if "azure-monitor" in args.trace_export:
        from azure.monitor.opentelemetry.exporter import AzureMonitorMetricExporter
        readers.append(PeriodicExportingMetricReader(
            AzureMonitorMetricExporter.from_connection_string(args.connection_string)))
    if "otlp" in args.trace_export:
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
        readers.append(PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=args.otlp_endpoint)))

    return memory_reader, readers


def configure_providers(args):
    """Wire the global tracer and meter providers and return the capture the assertions read.

    Only the providers. Turning on the instrumentor is left to each scenario, because that
    is the line worth seeing in a lab about tracing and because what it records depends on
    the run's own --sensitive-data.

    The providers have to exist before the first Foundry call either way — OpenTelemetry
    starts with a no-op provider, and spans handed to it are gone with no error to say so.
    """
    resource = Resource.create({"service.name": args.service_name})

    memory_exporter, processors = build_span_exporters(args)
    tracer_provider = TracerProvider(
        resource=resource,
        sampler=ParentBased(TraceIdRatioBased(args.sampling_ratio)),
    )
    for processor in processors:
        tracer_provider.add_span_processor(processor)
    trace.set_tracer_provider(tracer_provider)

    memory_reader, readers = build_metric_readers(args)
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=readers))

    return TraceCapture(memory_exporter, memory_reader, args.service_name)


def get_tracer():
    return trace.get_tracer(__name__)


def print_span_tree(spans):
    """Print the run as the shape it actually had.

    Anything whose parent is not in the capture is printed at the root rather than
    dropped. A span tree that silently loses spans is worse than no span tree — it reads
    as evidence that the missing work never happened.
    """
    rows = [to_dict(span) for span in spans]
    known = {row["span_id"] for row in rows}
    children = {}
    for row in rows:
        parent = row["parent_span_id"] if row["parent_span_id"] in known else None
        children.setdefault(parent, []).append(row)

    def walk(parent_id, depth):
        for row in children.get(parent_id, []):
            marker = " !" if row["status"] == "ERROR" else ""
            print(f"{'  ' * (depth + 1)}[{row['name']}]{marker}")
            walk(row["span_id"], depth + 1)

    print("\nspan tree")
    walk(None, 0)
    print(f"  {len(rows)} spans")


def query_app_insights(workspace_id, trace_id, credential):
    """Whether the trace reached Application Insights, polling until it does or we give up.

    Foundry stores traces in Application Insights, so this is the only check that proves
    the export path end to end rather than just the in-process pipeline. Ingestion lags
    the run by minutes, which is why this loops instead of asking once.
    """
    from azure.monitor.query import LogsQueryClient

    query = (f"union dependencies, traces, customEvents "
             f"| where operation_Id == '{trace_id}' | count")
    client = LogsQueryClient(credential)

    for attempt in range(1, APP_INSIGHTS_ATTEMPTS + 1):
        response = client.query_workspace(workspace_id, query, timespan=None)
        rows = response.tables[0].rows if response.tables else []
        found = rows[0][0] if rows else 0
        print(f"  [app insights attempt {attempt}/{APP_INSIGHTS_ATTEMPTS}] {found} rows")
        if found:
            return found
        if attempt < APP_INSIGHTS_ATTEMPTS:
            time.sleep(APP_INSIGHTS_INTERVAL_SECONDS)
    return 0


def build_tools(inject_error):
    """The lab's tools, as the two halves the Responses API needs them in.

    Returns the JSON schemas the model is shown, keyed by name, and the Python callables
    that answer them, keyed by the same names. There is no decorator turning a signature
    into a schema here on purpose: the schema is the contract with the model, and in a lab
    about what ends up in a trace it should be something you can read rather than infer.

    inject_error is closed over rather than read from a global, which keeps two runs in
    the same process — verify does exactly that — from seeing each other's setting.
    """

    def lookup_metric(service, metric):
        if inject_error and service == FAILING_SERVICE:
            raise RuntimeError(f"no telemetry is registered for service {service!r}")
        value = METRICS.get((service, metric))
        return value if value else f"no metric {metric!r} for service {service!r}"

    def list_incidents(service):
        incidents = INCIDENTS.get(service)
        if incidents is None:
            return f"service {service!r} is not known"
        return "\n".join(incidents) if incidents else f"no incidents for {service}"

    def compute_slo(good_events, total_events):
        if total_events <= 0:
            return "total_events must be positive"
        availability = good_events / total_events * 100
        budget_left = (availability - 99.9) / (100 - 99.9) * 100
        return f"availability {availability:.3f}%, error budget remaining {budget_left:.1f}%"

    schemas = {
        "lookup_metric": {
            "type": "function",
            "name": "lookup_metric",
            "description": "Read one metric for one service, with its value before the last release.",
            "parameters": {
                "type": "object",
                "properties": {
                    "service": {"type": "string", "description": "service name, e.g. 'checkout'"},
                    "metric": {"type": "string",
                               "description": "'p95_latency_ms', 'error_rate' or 'rps'"},
                },
                "required": ["service", "metric"],
                "additionalProperties": False,
            },
        },
        "list_incidents": {
            "type": "function",
            "name": "list_incidents",
            "description": "List today's incidents for a service, open and closed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "service": {"type": "string", "description": "service name, e.g. 'checkout'"},
                },
                "required": ["service"],
                "additionalProperties": False,
            },
        },
        "compute_slo": {
            "type": "function",
            "name": "compute_slo",
            "description": "Turn two counts into an availability figure and the error budget "
                           "left against 99.9%.",
            "parameters": {
                "type": "object",
                "properties": {
                    "good_events": {"type": "integer",
                                    "description": "requests that met the objective"},
                    "total_events": {"type": "integer", "description": "requests served"},
                },
                "required": ["good_events", "total_events"],
                "additionalProperties": False,
            },
        },
    }

    return schemas, {"lookup_metric": lookup_metric,
                     "list_incidents": list_incidents,
                     "compute_slo": compute_slo}


def build_task(args):
    """The task, plus the nudge that makes the failing tool call actually happen."""
    if not args.inject_error:
        return args.task
    return (f"{args.task} Before anything else, call lookup_metric for the service named "
            f"{FAILING_SERVICE} and report what happened when you did.")


@contextlib.contextmanager
def agent_versions(project, specs, model, schemas, keep=False):
    """Create one Foundry agent per spec, hand them back by name, delete them on the way out.

    This is where the create_agent spans come from — the instrumentor traces the create
    call, and gen_ai.agent.id on that span is how Foundry groups everything the agent
    later does. An agent that is never created is an agent the portal has no row for.

    Tools go on the agent definition rather than on the call. The service rejects a
    responses.create that carries both an agent reference and a tools list, so the agent
    is the only place a tool can be declared.

    Deleting is in a finally block because a failed run leaves the versions behind
    otherwise, and the next run would stack another version on top of them.

    keep turns the deleting off. It is for the portal rather than the trace — the spans
    reach Application Insights either way, but the portal lists agents that exist, so the
    tidiest run is also the one that leaves nothing to click. A kept run stacks a new
    version on the next pass, which is the price of being able to see it.
    """
    created = {}
    try:
        for spec in specs:
            created[spec["name"]] = project.agents.create_version(
                agent_name=f"{AGENT_PREFIX}{spec['name']}",
                definition=PromptAgentDefinition(
                    model=model,
                    instructions=spec["instructions"],
                    tools=[schemas[name] for name in spec["tools"]] or None,
                ),
            )
        yield created
    finally:
        if keep:
            for name, version in created.items():
                print(f"  kept agent {version.name} version {version.version}")
        else:
            for name, version in created.items():
                try:
                    project.agents.delete_version(agent_name=version.name,
                                                  agent_version=version.version, force=True)
                except Exception as error:  # noqa: BLE001 - one bad delete must not skip the rest
                    print(f"  could not delete agent {name} version {version.version}: {error}")


@contextlib.contextmanager
def conversation(client, keep):
    """Open a conversation, hand it back, delete it on the way out unless asked not to.

    The id is printed because it is what the portal keys its conversation view on, and a
    run worth keeping is a run you are about to go and look for.
    """
    created = client.conversations.create()
    print(f"  conversation {created.id}{' (kept)' if keep else ''}")
    try:
        yield created
    finally:
        if not keep:
            client.conversations.delete(conversation_id=created.id)


def agent_reference(agent):
    """How a responses.create says which agent should answer.

    The name in here is also what the instrumentor puts in the span name and in
    gen_ai.agent.name, so a run with no reference produces spans called plain "responses"
    that no one can attribute to an agent.
    """
    return {"agent_reference": {"type": "agent_reference", "name": agent.name, "id": agent.id}}


def run_tool_calls(response, agent, handlers, sensitive):
    """Answer every function call in one response, tracing each one. [] when the agent is done.

    The execute_tool span is ours because the tool ran here, not at the service — the
    instrumentor can see the model ask for the call and see the answer go back up, but the
    seconds in between belong to this process and nothing else would record them.

    These spans are siblings of the invoke_agent spans rather than children, because the
    model call that asked for the tool has already returned by the time the tool runs.
    That is the truth of the loop, but it leaves the tree unable to say whose tool this
    was — hence gen_ai.agent.name, which puts the answer on the span itself.
    """
    outputs = []
    for item in response.output:
        if item.type != "function_call":
            continue

        arguments = json.loads(item.arguments)
        with get_tracer().start_as_current_span(f"execute_tool {item.name}",
                                                kind=SpanKind.INTERNAL) as span:
            span.set_attribute("gen_ai.operation.name", "execute_tool")
            span.set_attribute("gen_ai.tool.name", item.name)
            span.set_attribute("gen_ai.tool.call.id", item.call_id)
            span.set_attribute("gen_ai.agent.name", agent.name)
            span.set_attribute("gen_ai.agent.id", agent.id)
            if sensitive:
                span.set_attribute("tool.call.arguments", json.dumps(arguments, ensure_ascii=False))

            started = time.perf_counter()
            try:
                result = handlers[item.name](**arguments)
            except Exception as error:  # noqa: BLE001 - the model gets to see and correct it
                span.set_status(Status(StatusCode.ERROR, str(error)))
                span.set_attribute("error.type", type(error).__name__)
                span.record_exception(error)
                result = f"the tool failed: {error}"
            TOOL_DURATION.record(time.perf_counter() - started,
                                 {"gen_ai.tool.name": item.name})

            if sensitive:
                span.set_attribute("tool.call.results", result)

        outputs.append({"type": "function_call_output", "call_id": item.call_id,
                        "output": result})
    return outputs


def run_turn(client, agent, conversation_id, prompt, handlers, sensitive, stop_on=None):
    """One agent's turn: ask, answer whatever tools it asked for, ask again, until it stops.

    Returns what the agent said and, when stop_on names a tool, the arguments it passed to
    that tool. The whole loop is a dozen lines because that is all a turn is. Everything a
    framework would have hidden — that a tool call is just another response item, that the
    answer goes back as ordinary input, that the conversation is what carries the history —
    is visible here, and each pass through the loop is one more agent span in the trace.

    stop_on exists for handoff, where one tool does not answer a question but ends the
    turn. It is still dispatched like any other tool, because leaving a function call
    unanswered in a conversation is how you get the next agent's request rejected.
    """
    response = client.responses.create(conversation=conversation_id, input=prompt,
                                       extra_body=agent_reference(agent))
    for _ in range(MAX_TOOL_ROUNDS):
        outputs = run_tool_calls(response, agent, handlers, sensitive)
        control = next((json.loads(item.arguments) for item in response.output
                        if item.type == "function_call" and item.name == stop_on), None)
        if not outputs:
            return response.output_text, control

        response = client.responses.create(conversation=conversation_id, input=outputs,
                                           extra_body=agent_reference(agent))
        if control:
            return response.output_text, control
    raise SystemExit(f"{agent.name} kept calling tools past {MAX_TOOL_ROUNDS} rounds")


def handoff(source, target, reason):
    """Record one agent passing the task to another.

    No SDK emits this. The service sees two unrelated calls; only this code knows the
    second happened because of the first, and the multi-agent semantic conventions name
    exactly this span so that knowledge does not stay in the code.
    https://learn.microsoft.com/en-us/azure/foundry/observability/concepts/trace-agent-concept
    """
    tracer = get_tracer()
    with tracer.start_as_current_span("agent_to_agent_interaction") as span:
        span.set_attribute("source.agent.name", source)
        span.set_attribute("target.agent.name", target)
        span.set_attribute("handoff.reason", reason)


def record_state(conversation_id, characters):
    """The conversation as the state the next agent inherits."""
    with get_tracer().start_as_current_span("agent.state.management") as span:
        span.set_attribute("memory.type", "short_term")
        span.set_attribute("gen_ai.conversation.id", conversation_id)
        span.set_attribute("memory.characters", characters)


def announce(args, root):
    """Say which trace this run is, before anything is spent on it.

    Printed rather than logged because the trace id is the one thing you need in your hand
    to go find this run in the portal afterwards.

    When nothing is going to Azure Monitor the trace id is still worth having — the
    service's own spans will be filed under it — but it no longer leads to this run's
    tree, and the warning says so before the run rather than after the portal disappoints.
    """
    trace_id = format_trace_id(root.get_span_context().trace_id)
    print(f"trace {trace_id}")
    print(f"  service {args.service_name}, "
          f"sensitive data {'on' if args.sensitive_data else 'off'}, "
          f"export {'+'.join(args.trace_export) or 'memory'}")

    if "azure-monitor" not in args.trace_export:
        print("  ! these spans are not going to Application Insights, so the portal will "
              "not show the scenario root or any span this lab names itself")
        print("  ! the service still records its own invoke_agent, chat and execute_tool "
              "spans under this trace id, rooted at parents that were never sent — pass "
              "--trace-export azure-monitor to send the other half and make it one tree")
    return trace_id


def report(args, capture):
    """Flush both pipelines, print the run as the shape it had, write the dump if asked.

    Called after the root span closes, never inside it — a span still open is a span the
    exporters have not been handed, and the tree would be missing its own root.
    """
    capture.flush()
    print_span_tree(capture.spans())
    if args.dump_spans:
        print(f"  wrote {capture.dump(args.dump_spans)}")
