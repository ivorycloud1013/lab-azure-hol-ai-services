"""Tracing plumbing and the agent cast for the hol-foundry-observability-*.py scripts.

Both live here because every scenario needs both and neither is what a scenario is
about. The orchestration — sequential, concurrent, handoff, group chat, magentic —
stays in each scenario script, because that is the one thing those files exist to show.

Providers are built by hand rather than through configure_otel_providers(). Hand-built
is the only way to attach an in-memory exporter *and* a network exporter *and* an
in-memory metric reader in one pass, and the in-memory pair is what
hol-foundry-observability-verify.py asserts against. See
https://learn.microsoft.com/en-us/agent-framework/agents/observability ("Manual setup").
"""

import asyncio
import json
import os
import time
from typing import Annotated

from agent_framework import Agent, AgentResponseUpdate, tool
from agent_framework.foundry import FoundryChatClient
from agent_framework.observability import create_resource, enable_instrumentation, get_tracer
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace import SpanKind, format_trace_id
from pydantic import Field

import identity

TRACE_EXPORTS = ["memory", "console", "azure-monitor", "otlp"]
DEFAULT_SERVICE_NAME = "hol-foundry-observability"
DEFAULT_OTLP_ENDPOINT = "http://localhost:4317"
DEFAULT_TASK = (
    "The checkout service got slower after this morning's release. "
    "Work out what changed, whether the error budget is still intact, "
    "and write a three-sentence summary an on-call engineer can act on."
)

# The tool the model is told to call when --inject-error is set. Anything else would
# make the failing span depend on the model inventing a bad argument on its own.
FAILING_SERVICE = "unknown-service"

# Traces reach Application Insights minutes after the run, not seconds — the ingestion
# pipeline batches. These bound the wait without turning a slow day into a false FAIL.
APP_INSIGHTS_ATTEMPTS = 10
APP_INSIGHTS_INTERVAL_SECONDS = 30

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
    return group


def validate_tracing_arguments(parser, args):
    """Reject impossible combinations before the first token is spent."""
    if not 0.0 <= args.sampling_ratio <= 1.0:
        parser.error("--sampling-ratio must be between 0.0 and 1.0")
    if "azure-monitor" in args.trace_export and not args.connection_string:
        parser.error("--trace-export azure-monitor needs --connection-string or "
                     "APPLICATIONINSIGHTS_CONNECTION_STRING. In the Foundry portal open "
                     "Agents > Traces and select Connect to attach an Application Insights "
                     "resource, then copy its connection string")


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


def configure(args):
    """Wire the global providers and return the capture the assertions read.

    OTEL_SERVICE_NAME is set before create_resource() because that is where the SDK
    reads it from — passing --service-name has to end up in the same place an operator
    setting the environment variable would.
    """
    os.environ["OTEL_SERVICE_NAME"] = args.service_name
    resource = create_resource()

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

    # Providers alone emit nothing — this is what turns on Agent Framework's own
    # instrumentation code paths, and what decides whether content lands in attributes.
    enable_instrumentation(enable_sensitive_data=args.sensitive_data)

    return TraceCapture(memory_exporter, memory_reader, args.service_name)


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
    """Fake telemetry tools, so a scenario costs one model call and no backend.

    inject_error is closed over rather than read from a global, which keeps two runs in
    the same process — verify does exactly that — from seeing each other's setting.
    """

    @tool(approval_mode="never_require")
    def lookup_metric(
        service: Annotated[str, Field(description="service name, e.g. 'checkout'")],
        metric: Annotated[str, Field(description="'p95_latency_ms', 'error_rate' or 'rps'")],
    ) -> str:
        """Read one metric for one service, with its value before the last release."""
        if inject_error and service == FAILING_SERVICE:
            raise RuntimeError(f"no telemetry is registered for service {service!r}")
        value = METRICS.get((service, metric))
        return value if value else f"no metric {metric!r} for service {service!r}"

    @tool(approval_mode="never_require")
    def list_incidents(
        service: Annotated[str, Field(description="service name, e.g. 'checkout'")],
    ) -> str:
        """List today's incidents for a service, open and closed."""
        incidents = INCIDENTS.get(service)
        if incidents is None:
            return f"service {service!r} is not known"
        return "\n".join(incidents) if incidents else f"no incidents for {service}"

    @tool(approval_mode="never_require")
    def compute_slo(
        good_events: Annotated[int, Field(description="requests that met the objective")],
        total_events: Annotated[int, Field(description="requests served")],
    ) -> str:
        """Turn two counts into an availability figure and the error budget left against 99.9%."""
        if total_events <= 0:
            return "total_events must be positive"
        availability = good_events / total_events * 100
        budget_left = (availability - 99.9) / (100 - 99.9) * 100
        return f"availability {availability:.3f}%, error budget remaining {budget_left:.1f}%"

    return [lookup_metric, list_incidents, compute_slo]


def build_cast(args):
    """Five agents on one chat client, returned by name so each scenario can pick the
    subset its pattern needs.

    A note that used to live here claimed reasoning models break every pattern that hands
    one agent's tool call to the next, with a "Stateless replay cannot reconstruct
    reasoning item(s)" exception. Running all five patterns end to end against
    gpt-5.6-terra on agent-framework-core 1.13.0 / orchestrations 1.0.2 did not reproduce
    it once, so the note is gone rather than left to be trusted. What actually blocked the
    lab was HandoffBuilder's require_per_service_call_history_persistence check, handled
    below. If the replay exception does come back, the framework's own answer to it is
    agent_framework_orchestrations._orchestrator_helpers.clean_conversation_for_handoff,
    which handoff and group chat already apply to every message they pass on; sequential
    is the pattern with no such guard, and AgentExecutor(context_mode="custom",
    context_filter=...) is where one would go.

    Every agent gets an explicit id. Foundry correlates traces from agents it does not
    host by gen_ai.agent.id on the create_agent span, so an agent without one is an
    agent whose spans the portal cannot group.
    """
    client = FoundryChatClient(
        project_endpoint=args.endpoint,
        model=args.deployment,
        credential=identity.get_credential(args),
    )
    tools = build_tools(args.inject_error)

    def make(name, description, instructions, agent_tools=None):
        """One agent, built the way every agent in this cast is built.

        The id is explicit and derived from the name so the two never drift apart.

        require_per_service_call_history_persistence is on for all five because
        HandoffBuilder.build() refuses to build without it on every participant — a handoff
        is a tool call that short-circuits the agent's turn, and the flag is what keeps the
        local history consistent with the service across that short-circuit. It costs the
        other four patterns nothing: the flag installs its middleware only when the agent
        has a HistoryProvider, and none of these do.
        """
        return Agent(
            client=client,
            id=f"hol-obs-{name}",
            name=name,
            description=description,
            instructions=instructions,
            tools=agent_tools,
            require_per_service_call_history_persistence=True,
        )

    return {
        "researcher": make(
            "researcher",
            "Pulls metrics and incidents out of the telemetry store.",
            "You gather facts with the tools you have. Report numbers, never opinions. "
            "Answer in the language of the question.",
            tools,
        ),
        "analyst": make(
            "analyst",
            "Turns raw telemetry into a cause and an error-budget verdict.",
            "You explain what the numbers mean and compute the error budget. "
            "Answer in the language of the question.",
            tools,
        ),
        "writer": make(
            "writer",
            "Writes the on-call summary.",
            "You write at most three sentences an on-call engineer can act on. "
            "Answer in the language of the question.",
        ),
        "reviewer": make(
            "reviewer",
            "Checks the summary against the evidence.",
            "You check every claim against what the other agents found and say "
            "APPROVED when it holds up. Answer in the language of the question.",
        ),
        "manager": make(
            "manager",
            "Coordinates the team.",
            "You coordinate the team to answer the task with as few turns as possible.",
        ),
    }


def build_task(args):
    """The task, plus the nudge that makes the failing tool call actually happen."""
    if not args.inject_error:
        return args.task
    return (f"{args.task} Before anything else, call lookup_metric for the service named "
            f"{FAILING_SERVICE} and report what happened when you did.")


def record_agent_creation(cast):
    """Open one create_agent span per agent in the cast.

    Agent Framework reserves this operation name but never opens the span for an agent you
    constructed yourself — it has nothing to report about a creation it did not perform.
    The span is still the one the convention puts gen_ai.agent.id on, and that id is how
    Foundry groups the traces of agents it does not host, so a run without it is a run the
    portal cannot attribute to an agent.

    It is emitted here rather than in build_cast because build_cast runs before the root
    span opens. A create_agent span started there would land in a trace of its own, which
    is the opposite of what an id meant for correlation is for.
    """
    tracer = get_tracer()
    for agent in cast.values():
        with tracer.start_as_current_span(f"create_agent {agent.name}", kind=SpanKind.CLIENT) as span:
            span.set_attribute("gen_ai.operation.name", "create_agent")
            span.set_attribute("gen_ai.agent.id", agent.id)
            span.set_attribute("gen_ai.agent.name", agent.name)
            if agent.description:
                span.set_attribute("gen_ai.agent.description", agent.description)


def run_scenario(args, run, title, cast):
    """The shell every scenario shares: configure, open a root span, run, report.

    The root span exists so a whole multi-agent run is one trace with one id. Without it
    each orchestration entry point would start its own trace and the portal would show
    the run as unrelated fragments.

    The cast is passed in only so its agents can be announced inside that root span.
    """
    capture = configure(args)

    with get_tracer().start_as_current_span(title, kind=SpanKind.CLIENT) as root:
        trace_id = format_trace_id(root.get_span_context().trace_id)
        print(f"trace {trace_id}")
        print(f"  service {args.service_name}, "
              f"sensitive data {'on' if args.sensitive_data else 'off'}, "
              f"export {'+'.join(args.trace_export) or 'memory'}")
        record_agent_creation(cast)
        asyncio.run(run(capture))

    capture.flush()
    print_span_tree(capture.spans())

    if args.dump_spans:
        print(f"  wrote {capture.dump(args.dump_spans)}")
    return trace_id


async def stream(workflow, task):
    """Run a workflow and print each participant's turn as it arrives.

    Streaming rather than awaiting the result, because a scenario about observability
    should let you watch the agents hand work to each other rather than show a summary
    once it is all over.
    """
    last_author = None
    events = workflow.run(task, stream=True)
    async for event in events:
        if event.type in ("intermediate", "output") and isinstance(event.data, AgentResponseUpdate):
            author = event.data.author_name or event.executor_id
            if author != last_author:
                print(f"\n  [{author}]", end=" ", flush=True)
                last_author = author
            print(event.data.text or "", end="", flush=True)
    print()
    return events
