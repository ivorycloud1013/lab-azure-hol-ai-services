#!/usr/bin/env python3
"""Client-side tracing for one agent, written with the Microsoft Agent Framework.

The same run as hol-foundry-observability-single.py, which does it against the raw
azure-ai-projects SDK. What changes is who runs the tool loop.

There, this file called responses.create, saw a function_call come back, ran the function,
and called responses.create again — so the trace showed two invoke_agent spans for what was
one question, and the tool span sat beside them as a sibling because the call that asked
for it had already returned.

Here agent.run() owns that loop, and the tracing follows the shape of the work:

  Scenario: single agent          the root span, ours
    invoke_agent weather-agent    one span for the whole question
      chat …                      one per model round trip, inside it
      execute_tool fetch_weather  a child of the invocation that asked for it

Nothing sets up an instrumentor here. The Agent Framework traces itself once providers
exist, which is what configure_otel_providers() does.
"""

import argparse
import asyncio
import os
from typing import Annotated

from agent_framework import Agent, tool
from agent_framework.foundry import FoundryChatClient
from agent_framework.observability import (
    configure_otel_providers,
    enable_instrumentation,
    get_tracer,
)
# The aio credential, not the one the other files use. Everything below is async, and the
# synchronous DefaultAzureCredential has no async context manager to enter.
from azure.identity.aio import DefaultAzureCredential
from pydantic import Field

QUESTION = "What is the weather in Seoul, and should I take an umbrella?"

WEATHER = {"Seoul": "rain, 18°C", "Busan": "cloudy, 21°C"}


# The framework builds the tool schema the model is shown out of this signature — the type
# hints, the Field descriptions and the docstring. There is no JSON schema to keep in step
# with the function, which is the other thing that changes against the raw SDK version.
#
# approval_mode is not about tracing. Without it the framework stops and asks before every
# call, which a script with no one watching cannot answer.
@tool(approval_mode="never_require")
def fetch_weather(
    city: Annotated[str, Field(description="city name, e.g. 'Seoul'")],
) -> str:
    """Get the current weather for a city."""
    return WEATHER.get(city, f"no weather for {city!r}")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Trace one Agent Framework agent against a Foundry model.",
        epilog="--export console prints the spans here and costs nothing. "
               "--export azure-monitor sends them to the Application Insights resource "
               "connected to the project — allow 2-5 minutes. Note that these are this "
               "process's spans: the framework talks to the model deployment, so there is "
               "no Foundry agent for the portal to file them under.",
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
                        help="record prompts, answers and tool arguments in the spans. "
                             "Development only.")
    parser.add_argument("--question", default=QUESTION)

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
        # Azure Monitor sets up the providers, so the framework must not also try. Asking
        # the project for its own connection string keeps this out of the environment.
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


async def main():
    args = parse_args()

    async with DefaultAzureCredential() as credential:
        configure_tracing(args)

        agent = Agent(
            name="weather-agent",
            client=FoundryChatClient(project_endpoint=args.endpoint, model=args.deployment,
                                     credential=credential),
            instructions="You are a concise weather assistant. Use the tool for facts.",
            tools=[fetch_weather],
        )

        # Without a root span the invocation is its own trace. One question makes that hard
        # to notice; it is the habit that matters once there is more than one call.
        with get_tracer().start_as_current_span("Scenario: single agent") as root:
            print(f"  trace {root.get_span_context().trace_id:032x}")

            async with agent:
                result = await agent.run(args.question)
            print(f"\n  {result.text}\n")

    if args.export == "azure-monitor":
        print("  sent to Application Insights — allow 2-5 minutes")


if __name__ == "__main__":
    asyncio.run(main())
