#!/usr/bin/env python3
"""Client-side tracing for one Foundry agent, in the order the docs put it.

https://learn.microsoft.com/en-us/azure/foundry/observability/how-to/trace-agent-client-side

One agent, one tool, one question. What the run should show:

  Scenario: single agent      the root span, ours
    create_agent …            the instrumentor's
    invoke_agent …            the instrumentor's, one per responses.create
    execute_tool …            ours, from answer_tool_calls

Sibling file: hol-foundry-observability-multi.py, the same setup with three agents.
"""

import argparse
import json
import os

# Read at import time by the telemetry module below, so it has to be set before it. Without
# it instrument() returns quietly and nothing is traced.
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

AGENT_NAME = "hol-obs-single"
QUESTION = "What is the weather in Seoul, and should I take an umbrella?"

WEATHER = {"Seoul": "rain, 18°C", "Busan": "cloudy, 21°C"}

WEATHER_TOOL = {
    "type": "function",
    "name": "fetch_weather",
    "description": "Get the current weather for a city.",
    "parameters": {
        "type": "object",
        "properties": {"city": {"type": "string", "description": "city name, e.g. 'Seoul'"}},
        "required": ["city"],
        "additionalProperties": False,
    },
}


# The tool runs here, not at the service, so the seconds it takes are only in this process.
# Plain function: the span around it is opened in answer_tool_calls, which is the only place
# that knows the id of the call being answered.
#
# The @trace_function decorator from azure.ai.projects.telemetry would also trace this, and
# it is what the docs reach for, but it describes the call as code rather than as a tool —
# see answer_tool_calls for what that costs. If you do use it, note that the parentheses are
# not optional: trace_function(span_name=None) is a factory, so the bare @trace_function the
# docs show binds the function to span_name and the next call fails on an unexpected keyword.
def fetch_weather(city: str) -> str:
    import time
    time.sleep(0.15)
    return WEATHER.get(city, f"no weather for {city!r}")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Trace one Foundry agent from the client side.",
        epilog="--export console prints the spans here and costs nothing. "
               "--export azure-monitor sends them to the Application Insights resource "
               "connected to the project, which is what makes them show up in the Foundry "
               "portal under Traces — allow 2-5 minutes.",
    )
    parser.add_argument("--endpoint", required=True, help="Foundry project endpoint")
    parser.add_argument("--deployment", default="gpt-5.6-terra", help="model deployment name")
    parser.add_argument("--export", choices=["console", "azure-monitor"],
                        default="azure-monitor")
    parser.add_argument("--question", default=QUESTION)
    parser.add_argument("--delete", action="store_true",
                        help="clean up the agent and the conversation on the way out. The "
                             "spans reach the exporter either way — this is for the portal, "
                             "which lists things that still exist. Left in place, the next "
                             "run stacks a new agent version on top.")

    return parser.parse_args()


def configure_tracing(args, project):
    """Point OpenTelemetry somewhere, then turn the instrumentor on. This order matters.

    OpenTelemetry starts with a no-op provider, and spans handed to that one disappear with
    no error to say so. So the exporter is set up first, and instrument() second.
    """
    if args.export == "azure-monitor":
        # The project knows its own Application Insights resource, so there is no connection
        # string to copy around. An unconnected project raises here.
        configure_azure_monitor(
            connection_string=project.telemetry.get_application_insights_connection_string())
    else:
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
        trace.set_tracer_provider(provider)

    # Off, and said out loud rather than left to the default: content recording puts user
    # messages and tool arguments in the telemetry, and this lab exports to a shared
    # Application Insights resource.
    AIProjectInstrumentor().instrument(enable_content_recording=False)


def answer_tool_calls(tracer, response, agent):
    """Run whatever functions the model asked for. [] means it is done talking to tools.

    The span is opened by hand rather than by @trace_function, and the difference is what
    the portal makes of it. The decorator records the call as code — code.function.parameter
    and code.function.return.value — with nothing saying what kind of operation it was, so
    the Traces view has no category to file it under and shows it as "other".

    What names the category is gen_ai.operation.name. Setting it to execute_tool, with the
    tool's name and the id of the call that asked for it, is what the GenAI semantic
    conventions ask for and what makes this appear as a tool call next to the model calls
    around it.
    """
    outputs = []
    for item in response.output:
        if item.type != "function_call":
            continue

        arguments = json.loads(item.arguments)
        with tracer.start_as_current_span(f"execute_tool {item.name}") as span:
            span.set_attribute("gen_ai.operation.name", "execute_tool")
            span.set_attribute("gen_ai.tool.name", item.name)
            span.set_attribute("gen_ai.tool.type", "function")
            span.set_attribute("gen_ai.tool.call.id", item.call_id)
            span.set_attribute("gen_ai.agent.name", agent.name)
            span.set_attribute("gen_ai.agent.id", agent.id)

            result = fetch_weather(**arguments)

        outputs.append({"type": "function_call_output", "call_id": item.call_id,
                        "output": result})
    return outputs


def main():
    args = parse_args()

    with (
        DefaultAzureCredential() as credential,
        AIProjectClient(endpoint=args.endpoint, credential=credential,
                        allow_preview=True) as project,
    ):
        configure_tracing(args, project)
        tracer = trace.get_tracer(__name__)

        # Without a root span every call starts its own trace, and the portal shows the run
        # as unrelated fragments instead of one thing that happened.
        with tracer.start_as_current_span("Scenario: single agent") as root:
            print(f"  trace {trace.format_trace_id(root.get_span_context().trace_id)}")

            agent = project.agents.create_version(
                agent_name=AGENT_NAME,
                definition=PromptAgentDefinition(
                    model=args.deployment,
                    instructions="You are a concise weather assistant. Use the tool for facts.",
                    # Tools go on the agent, not on the call — the service rejects a
                    # responses.create that carries an agent reference and a tools list.
                    tools=[WEATHER_TOOL],
                ),
            )
            print(f"  agent {agent.name} version {agent.version}"
                  f"{'' if args.delete else ' (kept)'}")

            # The name and id here are what the instrumentor writes into gen_ai.agent.*,
            # and they are how the portal ties these spans to that agent's row.
            reference = {"agent_reference": {"type": "agent_reference",
                                             "name": agent.name, "id": agent.id}}

            with project.get_openai_client() as client:
                conversation = client.conversations.create()
                print(f"  conversation {conversation.id}"
                      f"{'' if args.delete else ' (kept)'}")
                try:
                    response = client.responses.create(conversation=conversation.id,
                                                       input=args.question,
                                                       extra_body=reference)
                    outputs = answer_tool_calls(tracer, response, agent)
                    if outputs:
                        # A tool answer goes back as ordinary input, which is a second
                        # responses.create and therefore a second invoke_agent span.
                        response = client.responses.create(conversation=conversation.id,
                                                           input=outputs, extra_body=reference)
                    print(f"\n  {response.output_text}\n")
                finally:
                    if args.delete:
                        client.conversations.delete(conversation_id=conversation.id)

            if args.delete:
                project.agents.delete_version(agent_name=agent.name,
                                              agent_version=agent.version, force=True)

    if args.export == "azure-monitor":
        print("  sent to Application Insights — Foundry portal > Traces, 2-5 minutes")


if __name__ == "__main__":
    main()
