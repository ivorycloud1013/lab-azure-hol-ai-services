#!/usr/bin/env python3
"""Client-side tracing for three agents in sequence, with the Microsoft Agent Framework.

The same pipeline as hol-foundry-observability-multi.py, which does it against the raw
azure-ai-projects SDK. What changes is who knows it is a pipeline.

There, this file called three agents in a loop and had to open agent_to_agent_interaction
spans itself, because nothing else in the system knew the three calls were related.

Here SequentialBuilder is the pipeline, so the structure is in the trace without anyone
writing it down — each agent's span is a child of the workflow's, and the conversation is
passed along by the workflow rather than by a conversation id this file carries:

  Scenario: multi agent          the root span, ours
    workflow                     the framework's
      invoke_agent researcher
      invoke_agent analyst
      invoke_agent writer

Nothing sets up an instrumentor here. The Agent Framework traces itself once providers
exist, which is what configure_otel_providers() does.
"""

import argparse
import asyncio
import os

from agent_framework import Agent
from agent_framework.foundry import FoundryChatClient
from agent_framework.observability import (
    configure_otel_providers,
    enable_instrumentation,
    get_tracer,
)
from agent_framework.orchestrations import SequentialBuilder
# The aio credential, not the one the other files use. Everything below is async, and the
# synchronous DefaultAzureCredential has no async context manager to enter.
from azure.identity.aio import DefaultAzureCredential

TASK = ("Checkout p95 latency went from 180 ms to 900 ms after this morning's release. "
        "Say what happened and what to do about it.")

# One entry per agent, in the order they run. The workflow hands each of them everything
# said so far, so an instruction only has to say what this agent adds to it.
PIPELINE = [
    ("researcher", "You collect facts. List what you would need to diagnose this. "
                   "Numbers only, no prose."),
    ("analyst", "You diagnose. Work only from what the researcher listed, and name the "
                "likely cause."),
    ("writer", "You write for on-call engineers. Six lines at most, from what the other "
               "two established."),
]


def parse_args():
    parser = argparse.ArgumentParser(
        description="Trace three Agent Framework agents running in sequence.",
        epilog="--export console prints the spans here and costs nothing. "
               "--export azure-monitor sends them to the Application Insights resource "
               "connected to the project — allow 2-5 minutes.",
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
                        help="record prompts and answers in the spans. Development only.")
    parser.add_argument("--task", default=TASK)

    args = parser.parse_args()
    if not args.endpoint:
        parser.error("--endpoint or FOUNDRY_PROJECT_ENDPOINT is required")
    return args


def configure_tracing(args):
    """Set up exporters and turn instrumentation on. Once, before anything is traced.

    Two ways in, because only one provider per signal can exist. Either the framework builds
    the providers, or something else does and the framework is told to use them.
    """
    if args.export == "azure-monitor":
        # This lookup is the one synchronous call in the file, so it brings its own
        # synchronous credential rather than borrowing the async one.
        from azure.ai.projects import AIProjectClient
        from azure.identity import DefaultAzureCredential as SyncCredential
        from azure.monitor.opentelemetry import configure_azure_monitor

        with (
            SyncCredential() as credential,
            AIProjectClient(endpoint=args.endpoint, credential=credential,
                            allow_preview=True) as project,
        ):
            configure_azure_monitor(
                connection_string=project.telemetry.get_application_insights_connection_string())
        enable_instrumentation(enable_sensitive_data=args.content)
    else:
        configure_otel_providers(enable_console_exporters=True,
                                 enable_sensitive_data=args.content)


def build_agents(endpoint, deployment, credential):
    """One agent per pipeline entry, all three on the same model deployment."""
    return [
        Agent(
            name=name,
            client=FoundryChatClient(project_endpoint=endpoint, model=deployment,
                                     credential=credential),
            instructions=instructions,
        )
        for name, instructions in PIPELINE
    ]


def print_conversation(result):
    """What the pipeline produced, which is the last agent's answer.

    The turns before it are not in here — SequentialBuilder takes intermediate_output_from
    if you want them printed. They are in the trace either way, one invoke_agent span each,
    which is the reason this file exists.
    """
    for output in result.get_outputs():
        # One AgentResponse for the run. The turns are its messages, and the author is on
        # each message rather than on the response.
        for message in getattr(output, "messages", None) or [output]:
            text = getattr(message, "text", "").strip()
            if not text:
                continue
            role = getattr(message, "role", None)
            label = (getattr(message, "author_name", None)
                     or getattr(role, "value", None) or role)
            print(f"\n  [{label}] {text}")


async def main():
    args = parse_args()

    async with DefaultAzureCredential() as credential:
        configure_tracing(args)

        agents = build_agents(args.endpoint, args.deployment, credential)
        workflow = SequentialBuilder(participants=agents).build()

        with get_tracer().start_as_current_span("Scenario: multi agent") as root:
            print(f"  trace {root.get_span_context().trace_id:032x}")
            print(f"  pipeline {' -> '.join(name for name, _ in PIPELINE)}")

            result = await workflow.run(args.task)
            print_conversation(result)

    if args.export == "azure-monitor":
        print("\n  sent to Application Insights — allow 2-5 minutes")


if __name__ == "__main__":
    asyncio.run(main())
