#!/usr/bin/env python3
"""One trace across HTTP hops — three agents behind a local server, called in turn.

The other scripts keep everything in one process, where the trace holds together because
OpenTelemetry's context is a context variable that every call inherits. That is not
propagation. The moment an agent is called over HTTP the context does not go with it, and
you get one trace per service instead of one trace per task.

Here the three agents are served by a local HTTP server on a background thread, so each
call really crosses a socket. The handler runs on a thread the request created and starts
with no context — which is what makes this a test and not a demonstration of nothing.

  Scenario: pipeline over HTTP
    POST /researcher           the caller's side of the hop
      handle /researcher       the server's side, reconnected by extract()
        invoke_agent …         the SDK's
    POST /analyst
      …

Run it once as is, then again with --no-propagate. The verdict printed at the end flips.

This is not the AZURE_TRACING_GEN_AI_* propagation, which only stamps the requests the SDK
sends to Foundry itself. Between your own services, inject and extract are what carry the
trace, and baggage is what carries anything else.
"""

import argparse
import json
import os
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ["AZURE_EXPERIMENTAL_ENABLE_GENAI_TRACING"] = "true"

from azure.core.settings import settings  # noqa: E402

settings.tracing_implementation = "opentelemetry"

from azure.ai.projects import AIProjectClient  # noqa: E402
from azure.ai.projects.models import PromptAgentDefinition  # noqa: E402
from azure.ai.projects.telemetry import AIProjectInstrumentor  # noqa: E402
from azure.identity import DefaultAzureCredential  # noqa: E402
from azure.monitor.opentelemetry import configure_azure_monitor  # noqa: E402
from opentelemetry import baggage, context, trace  # noqa: E402
from opentelemetry.propagate import extract, inject  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor  # noqa: E402

TASK = "Checkout p95 latency tripled after this morning's release."
SESSION_ID = "hol-session-42"

PIPELINE = [
    ("researcher", "List the facts you would need to diagnose this. One line."),
    ("analyst", "Name the likely cause from what you are given. One line."),
    ("writer", "Write two lines for on-call engineers."),
]

# What the server saw, one entry per request. Read after the run to write the verdict.
HOPS = []


def parse_args():
    parser = argparse.ArgumentParser(
        description="Carry one trace across HTTP hops between three agents.",
        epilog="--no-propagate drops the headers, which is the control: every hop then "
               "starts its own trace.",
    )
    parser.add_argument("--endpoint",
                        default=os.getenv("FOUNDRY_PROJECT_ENDPOINT")
                        or os.getenv("AZURE_AI_PROJECT_ENDPOINT"),
                        help="Foundry project endpoint")
    parser.add_argument("--deployment",
                        default=os.getenv("FOUNDRY_MODEL_NAME")
                        or os.getenv("AZURE_AI_MODEL_DEPLOYMENT_NAME", "gpt-5.6-terra"),
                        help="model deployment name")
    parser.add_argument("--export", choices=["console", "azure-monitor"], default="console")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--no-propagate", action="store_true",
                        help="send no trace headers")
    parser.add_argument("--keep", action="store_true",
                        help="leave the three agents behind. The spans reach the exporter "
                             "either way — this is for the portal, which lists agents that "
                             "still exist. A kept run stacks a new version on the next pass.")

    args = parser.parse_args()
    if not args.endpoint:
        parser.error("--endpoint or FOUNDRY_PROJECT_ENDPOINT is required")
    return args


def configure_tracing(args, project):
    """Exporters first, instrumentor second — spans given to the default no-op provider are
    dropped with no error to say so."""
    if args.export == "azure-monitor":
        # sampling_ratio=1.0 keeps every trace. Left unset, Azure Monitor installs a sampler
        # that can decide this trace is not worth recording, and a dropped trace here is not
        # merely invisible: the spans become NonRecordingSpan, the sampled bit in the
        # traceparent we inject goes to 00, and the SDK's own instrumentor then crashes
        # reading .attributes off a span that has none. Full sampling is what a lab wants
        # anyway — a run you cannot see is a run you cannot learn from.
        configure_azure_monitor(
            connection_string=project.telemetry.get_application_insights_connection_string(),
            sampling_ratio=1.0)
    else:
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
        trace.set_tracer_provider(provider)

    AIProjectInstrumentor().instrument()


def ask(client, agent, prompt):
    response = client.responses.create(
        input=prompt,
        extra_body={"agent_reference": {"type": "agent_reference",
                                        "name": agent.name, "id": agent.id}},
    )
    return response.output_text


def build_handler(client, agents, tracer):
    """The server side: answer with the agent that was asked for, on the caller's trace."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            name = payload["agent"]

            # The one line that matters. extract() rebuilds the caller's context out of the
            # traceparent and baggage headers; passing it to the span makes this span the
            # caller's child across the socket. With no headers it yields an empty context
            # and the span below starts a trace of its own.
            #
            # The keys are lowercased first, and that is not tidying. HTTP header names are
            # case insensitive and urllib sends them capitalised — Traceparent, Baggage —
            # but the default getter looks the name up in this dict verbatim, in lowercase.
            # Hand it the headers as they arrived and it finds nothing, silently: every hop
            # then starts its own trace and the baggage arrives empty, with no error saying
            # why. Whatever client you use, lowercase before extracting.
            ctx = extract({key.lower(): value for key, value in self.headers.items()})
            caller = trace.get_current_span(ctx).get_span_context()

            with tracer.start_as_current_span(f"handle /{name}", context=ctx) as span:
                span.set_attribute("service.role", "agent-host")
                HOPS.append({
                    "agent": name,
                    "trace_id": f"{span.get_span_context().trace_id:032x}",
                    "parent": f"{caller.span_id:016x}" if caller.is_valid else None,
                    # Baggage rides with the context, which is how a value set once at the
                    # edge reaches every service without being passed down by hand.
                    "baggage": dict(baggage.get_all(ctx)),
                })
                text = ask(client, agents[name], payload["prompt"])

            body = json.dumps({"text": text}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            """The access log would drown the spans."""

    return Handler


def call(tracer, port, name, prompt, propagate):
    """The caller's side: open a span, write it into the headers, send it."""
    with tracer.start_as_current_span(f"POST /{name}") as span:
        headers = {"Content-Type": "application/json"}
        if propagate:
            # Serialises the current span and the current baggage into W3C headers.
            inject(headers)

        request = urllib.request.Request(f"http://127.0.0.1:{port}/{name}", method="POST",
                                         data=json.dumps({"agent": name,
                                                          "prompt": prompt}).encode(),
                                         headers=headers)
        with urllib.request.urlopen(request, timeout=120) as response:
            text = json.loads(response.read())["text"]

        return text, f"{span.get_span_context().span_id:016x}"


def report(trace_id, sent):
    """Whether each hop landed in the caller's trace, or started its own."""
    print(f"\n  --- caller trace {trace_id} ---")
    for hop, span_id in zip(HOPS, sent):
        joined = hop["trace_id"] == trace_id and hop["parent"] == span_id
        print(f"  {hop['agent']:<11} trace {hop['trace_id']} "
              f"parent {hop['parent'] or '(none)'} -> {'joined' if joined else 'SEPARATE'}")
    print(f"  baggage seen by the server: {HOPS[0]['baggage'] if HOPS else '(none)'}")


def main():
    args = parse_args()

    with (
        DefaultAzureCredential() as credential,
        AIProjectClient(endpoint=args.endpoint, credential=credential,
                        allow_preview=True) as project,
    ):
        configure_tracing(args, project)
        tracer = trace.get_tracer(__name__)

        agents = {
            name: project.agents.create_version(
                agent_name=f"hol-obs-hop-{name}",
                definition=PromptAgentDefinition(model=args.deployment,
                                                 instructions=instructions))
            for name, instructions in PIPELINE
        }

        with project.get_openai_client() as client:
            server = ThreadingHTTPServer(("127.0.0.1", args.port),
                                         build_handler(client, agents, tracer))
            # Request handlers run on non-daemon threads by default, and a single one still
            # alive at the end keeps the interpreter from exiting. This script is done the
            # moment the last answer is printed, so nothing here is worth waiting for.
            server.daemon_threads = True
            # A daemon thread, which is the point: the handler inherits no context from the
            # thread that sends the request, exactly like a separate process would not.
            threading.Thread(target=server.serve_forever, daemon=True).start()
            print(f"  agents served on 127.0.0.1:{args.port}")

            # Baggage has to be in the context before inject() runs, so it goes on around
            # the whole scenario rather than inside it.
            token = context.attach(baggage.set_baggage("session.id", SESSION_ID))
            try:
                with tracer.start_as_current_span("Scenario: pipeline over HTTP") as root:
                    trace_id = f"{root.get_span_context().trace_id:032x}"
                    # Recording false means a sampler dropped this trace. Everything below
                    # still runs and still carries ids, but nothing is written down.
                    print(f"  trace {trace_id} (recording: {root.is_recording()})")

                    sent, prompt = [], TASK
                    for name, _ in PIPELINE:
                        text, span_id = call(tracer, args.port, name, prompt,
                                             not args.no_propagate)
                        print(f"\n  [{name}] {text}")
                        sent.append(span_id)
                        prompt = f"{TASK}\n\nSo far: {text}"
            finally:
                context.detach(token)
                # shutdown() only stops the accept loop. Without server_close() the
                # listening socket stays open, and the next run cannot bind the port.
                server.shutdown()
                server.server_close()

        for agent in agents.values():
            if args.keep:
                print(f"  kept agent {agent.name} version {agent.version}")
                continue
            try:
                project.agents.delete_version(agent_name=agent.name,
                                              agent_version=agent.version, force=True)
            except Exception as error:  # noqa: BLE001 - one bad delete must not skip the rest
                print(f"  could not delete {agent.name}: {error}")

    report(trace_id, sent)

    # Flush and stop the exporter threads on purpose. A short-lived script that just exits
    # leaves the last spans in the batch queue, and with Azure Monitor the shutdown at exit
    # can sit there waiting on the network with nothing to show for it.
    provider = trace.get_tracer_provider()
    if hasattr(provider, "shutdown"):
        provider.shutdown()


if __name__ == "__main__":
    main()
