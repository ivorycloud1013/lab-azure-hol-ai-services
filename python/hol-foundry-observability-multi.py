#!/usr/bin/env python3
"""Client-side tracing for three Foundry agents working in sequence.

https://learn.microsoft.com/en-us/azure/foundry/observability/how-to/trace-agent-client-side

The same setup as hol-foundry-observability-single.py, with the one thing a single agent
cannot show: the service sees three unrelated calls, and only this file knows they were a
pipeline. The agent_to_agent_interaction spans are how that knowledge gets into the trace.

  Scenario: multi agent          the root span, ours
    create_agent × 3             the instrumentor's
    invoke_agent researcher      the instrumentor's
    agent_to_agent_interaction   ours — researcher handed off to analyst
    invoke_agent analyst
    agent_to_agent_interaction
    invoke_agent writer
"""

import argparse
import os

os.environ["AZURE_EXPERIMENTAL_ENABLE_GENAI_TRACING"] = "true"

from azure.core.settings import settings  # noqa: E402

# The docs leave this out, and without it the run still looks fine — you get the spans this
# file opens itself and none of the SDK's. AIProjectInstrumentor draws create_agent and
# invoke_agent through azure-core's tracing abstraction, which is a no-op until this line
# picks OpenTelemetry as its implementation. It has to come before any azure-core client.
settings.tracing_implementation = "opentelemetry"

from azure.ai.projects import AIProjectClient  # noqa: E402
from azure.ai.projects.models import PromptAgentDefinition  # noqa: E402
from azure.ai.projects.telemetry import AIProjectInstrumentor  # noqa: E402
from azure.identity import DefaultAzureCredential  # noqa: E402
from azure.monitor.opentelemetry import configure_azure_monitor  # noqa: E402
from opentelemetry import trace  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor  # noqa: E402

TASK = ("Checkout p95 latency went from 180 ms to 900 ms after this morning's release. "
        "Say what happened and what to do about it.")

# One entry per agent, in the order they run. Instructions say what to do with what the
# previous agent said — the conversation already carries the said part, so repeating it
# here would only pay input tokens to tell an agent something it can already read.
PIPELINE = [
    ("researcher", "You collect facts. List what you would need to diagnose this. Numbers only.",
     "Collect the metrics and incidents this needs."),
    ("analyst", "You diagnose. Work only from what the researcher listed.",
     "Name the likely cause, from what the researcher listed."),
    ("writer", "You write for on-call engineers. Six lines at most.",
     "Write the summary, from what the other two established."),
]


def parse_args():
    parser = argparse.ArgumentParser(
        description="Trace three Foundry agents running in sequence, from the client side.",
        epilog="--export console prints the spans here and costs nothing. "
               "--export azure-monitor sends them to the Application Insights resource "
               "connected to the project, which is what makes them show up in the Foundry "
               "portal under Traces — allow 2-5 minutes.",
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
    parser.add_argument("--content", action="store_true",
                        help="record prompts and answers in the spans. Development only — "
                             "this puts user messages in your telemetry.")
    parser.add_argument("--task", default=TASK)
    parser.add_argument("--keep", action="store_true",
                        help="leave the three agents and the conversation behind. The spans "
                             "reach the exporter either way — this is for the portal, which "
                             "lists things that still exist. A kept run stacks a new agent "
                             "version on the next pass.")

    args = parser.parse_args()
    if not args.endpoint:
        parser.error("--endpoint or FOUNDRY_PROJECT_ENDPOINT is required")
    return args


def configure_tracing(args, project):
    """Point OpenTelemetry somewhere, then turn the instrumentor on. This order matters.

    OpenTelemetry starts with a no-op provider, and spans handed to that one disappear with
    no error to say so. So the exporter is set up first, and instrument() second.
    """
    if args.export == "azure-monitor":
        configure_azure_monitor(
            connection_string=project.telemetry.get_application_insights_connection_string())
    else:
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
        trace.set_tracer_provider(provider)

    AIProjectInstrumentor().instrument(enable_content_recording=args.content)


def create_agents(project, deployment):
    """One Foundry agent per pipeline entry, keyed by name."""
    return {
        name: project.agents.create_version(
            agent_name=f"hol-obs-multi-{name}",
            definition=PromptAgentDefinition(model=deployment, instructions=instructions),
        )
        for name, instructions, _ in PIPELINE
    }


def delete_agents(project, agents, keep):
    if keep:
        for agent in agents.values():
            print(f"  kept agent {agent.name} version {agent.version}")
        return

    for agent in agents.values():
        try:
            project.agents.delete_version(agent_name=agent.name, agent_version=agent.version,
                                          force=True)
        except Exception as error:  # noqa: BLE001 - one bad delete must not skip the rest
            print(f"  could not delete {agent.name}: {error}")


def record_handoff(tracer, source, target, reason):
    """One agent passing the task to another. No SDK emits this — the service never saw it."""
    with tracer.start_as_current_span("agent_to_agent_interaction") as span:
        span.set_attribute("source.agent.name", source)
        span.set_attribute("target.agent.name", target)
        span.set_attribute("handoff.reason", reason)


def main():
    args = parse_args()

    with (
        DefaultAzureCredential() as credential,
        AIProjectClient(endpoint=args.endpoint, credential=credential,
                        allow_preview=True) as project,
    ):
        configure_tracing(args, project)
        tracer = trace.get_tracer(__name__)

        # Without a root span each turn starts its own trace, and the portal shows three
        # unrelated calls instead of one pipeline.
        with tracer.start_as_current_span("Scenario: multi agent") as root:
            print(f"  trace {trace.format_trace_id(root.get_span_context().trace_id)}")
            print(f"  pipeline {' -> '.join(name for name, _, _ in PIPELINE)}")

            agents = create_agents(project, args.deployment)
            try:
                with project.get_openai_client() as client:
                    # One conversation for all three. That is what carries the history, so
                    # each agent can read what the previous one said without being told.
                    conversation = client.conversations.create()
                    print(f"  conversation {conversation.id}"
                          f"{' (kept)' if args.keep else ''}")
                    try:
                        run_pipeline(client, tracer, agents, conversation.id, args.task)
                    finally:
                        if not args.keep:
                            client.conversations.delete(conversation_id=conversation.id)
            finally:
                delete_agents(project, agents, args.keep)

    if args.export == "azure-monitor":
        print("  sent to Application Insights — Foundry portal > Traces, 2-5 minutes")


def run_pipeline(client, tracer, agents, conversation_id, task):
    """Each agent takes one turn on the shared conversation, in order."""
    previous = None
    for name, _, step in PIPELINE:
        if previous:
            record_handoff(tracer, previous, name, step)

        # The task rides on the first turn only. After that the conversation holds it.
        prompt = f"task: {task}\n\n{step}" if previous is None else step
        agent = agents[name]
        response = client.responses.create(
            conversation=conversation_id,
            input=prompt,
            extra_body={"agent_reference": {"type": "agent_reference",
                                            "name": agent.name, "id": agent.id}},
        )
        print(f"\n  [{name}] {response.output_text}")
        previous = name


if __name__ == "__main__":
    main()
