#!/usr/bin/env python3

import argparse
import os

from azure.ai.projects import AIProjectClient
from azure.ai.projects.telemetry import AIProjectInstrumentor
from opentelemetry.trace import SpanKind

import identity
import observability

PIPELINE = ["researcher", "analyst", "writer"]

# What each agent is told when its turn comes. The conversation carries what the previous
# agents said, so these say what to do with it and nothing more — repeating the findings
# here would pay input tokens to tell an agent something it can already read.
STEPS = {
    "researcher": "Collect the metrics and incidents this needs. Numbers only.",
    "analyst": "Using what the researcher found, name the likely cause and judge the error budget.",
    "writer": "Write the summary the task asked for, from what the other two established.",
}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Trace a sequential multi-agent pipeline on Foundry — researcher, then "
                    "analyst, then writer, each building on the last.",
        epilog="The simplest shape to read in a trace, and the one to start from. Three agents "
               "share one conversation, so each turn is one responses.create against the same "
               "conversation id with a different agent named in it, and the invoke_agent spans "
               "land in the order the loop ran them. The agent_to_agent_interaction and "
               "agent.state.management spans between the turns are ours — the service sees "
               "three unrelated calls and only this file knows they were a pipeline. "
               "Sibling scenarios: -concurrent (the same agents answering at once), -handoff "
               "(the agents choose who goes next), -semconv (the whole coordination vocabulary). "
               "hol-foundry-observability-verify.py runs all of them and asserts what each "
               "should have emitted. To see this run in the Foundry portal under "
               "Agents > Traces, pass --trace-export azure-monitor — the service traces its "
               "own calls once Application Insights is connected, but the spans above are "
               "this process's and go nowhere without it.",
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
    observability.add_cast_arguments(parser)

    args = parser.parse_args()
    if not args.endpoint:
        parser.error("--endpoint or FOUNDRY_PROJECT_ENDPOINT is required")
    if args.auth in ("api-key", "access-token"):
        parser.error(f"--auth {args.auth} is not supported by this SDK path, use another method")
    observability.validate_tracing_arguments(parser, args)
    return args


def main():
    args = parse_args()
    task = observability.build_task(args)
    schemas, handlers = observability.build_tools(args.inject_error)

    # Providers first — OpenTelemetry starts with a no-op one, and spans handed to that are
    # gone with no error to say so.
    capture = observability.configure_providers(args)
    tracer = observability.get_tracer()

    # GenAI tracing is off unless this variable says otherwise, and instrument() reads it
    # and returns quietly when it is missing. Set it after the providers and before the
    # instrumentor, which is the only order in which both of them take effect.
    os.environ["AZURE_EXPERIMENTAL_ENABLE_GENAI_TRACING"] = "true"
    AIProjectInstrumentor().instrument(enable_content_recording=args.sensitive_data)

    with (
        # The root span. Without it each turn starts its own trace and the portal shows the
        # run as unrelated fragments instead of one pipeline.
        tracer.start_as_current_span("Scenario: sequential", kind=SpanKind.CLIENT) as root,
        identity.get_credential(args) as credential,
        AIProjectClient(endpoint=args.endpoint, credential=credential, allow_preview=True) as project,
        observability.agent_versions(project, observability.specs_for(*PIPELINE),
                                     args.deployment, schemas) as agents,
        project.get_openai_client() as client,
    ):
        observability.announce(args, root)
        print(f"  pipeline {' -> '.join(PIPELINE)}")

        conversation = client.conversations.create()
        print(f"  conversation {conversation.id}")
        try:
            # The task rides on the first turn only. After that the conversation holds it.
            prompt = f"task: {task}\n\n{STEPS[PIPELINE[0]]}"
            previous = None
            transcript = ""

            for name in PIPELINE:
                if previous:
                    observability.handoff(previous, name, STEPS[name])
                    observability.record_state(conversation.id, len(transcript))
                    prompt = STEPS[name]

                text, _ = observability.run_turn(client, agents[name], conversation.id, prompt,
                                                 handlers, args.sensitive_data)
                print(f"\n  [{name}] {text}")
                transcript += text
                previous = name
        finally:
            client.conversations.delete(conversation_id=conversation.id)

    observability.report(args, capture)


if __name__ == "__main__":
    main()
