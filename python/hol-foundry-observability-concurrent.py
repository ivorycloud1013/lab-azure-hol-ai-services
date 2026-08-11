#!/usr/bin/env python3

import argparse
import asyncio
import os

from azure.ai.projects import AIProjectClient
from azure.ai.projects.telemetry import AIProjectInstrumentor
from opentelemetry import context as otel_context
from opentelemetry.trace import SpanKind

import identity
import observability

BRANCHES = ["researcher", "analyst", "reviewer"]
AGGREGATOR = "writer"

# Each branch answers the whole task from its own angle. They cannot see each other, which
# is the point of the pattern and also why each one gets its own conversation.
ANGLES = {
    "researcher": "Answer this from the telemetry alone. Numbers and incidents, no interpretation.",
    "analyst": "Answer this as a diagnosis. Name a cause and judge the error budget.",
    "reviewer": "Answer this as a risk check. Say what could be concluded too early and what is missing.",
}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Trace a concurrent multi-agent fan-out on Foundry — three agents answer "
                    "the same task in parallel and a fourth merges the answers.",
        epilog="This is the scenario where the trace says something the transcript cannot: the "
               "three branch spans overlap in time, and the aggregator's span begins only after "
               "the last of them ends. Compare the wall time here against -sequential over the "
               "same task. Each branch gets its own conversation because a shared one would let "
               "the branches read each other, which is the one thing a fan-out must not do — "
               "-sequential is the scenario that shares one on purpose. The SDK calls block, so "
               "the fan-out is asyncio.to_thread rather than a second async client surface.",
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


def branch(client, agent, task, angle, handlers, sensitive):
    """One branch, start to finish, on a conversation nobody else touches."""
    conversation = client.conversations.create()
    try:
        text, _ = observability.run_turn(client, agent, conversation.id,
                                         f"task: {task}\n\n{angle}", handlers, sensitive)
        return text
    finally:
        client.conversations.delete(conversation_id=conversation.id)


async def fan_out(client, agents, task, handlers, sensitive):
    """Every branch at once.

    The OpenTelemetry context does not follow a thread on its own, so each worker attaches
    the run's context before it starts. Without that every branch span would begin a trace
    of its own and the fan-out would read as three unrelated runs.
    """
    current = otel_context.get_current()

    def in_context(name):
        token = otel_context.attach(current)
        try:
            return branch(client, agents[name], task, ANGLES[name], handlers, sensitive)
        finally:
            otel_context.detach(token)

    return await asyncio.gather(*(asyncio.to_thread(in_context, name) for name in BRANCHES))


def main():
    args = parse_args()
    task = observability.build_task(args)
    schemas, handlers = observability.build_tools(args.inject_error)

    capture = observability.configure_providers(args)
    tracer = observability.get_tracer()

    os.environ["AZURE_EXPERIMENTAL_ENABLE_GENAI_TRACING"] = "true"
    AIProjectInstrumentor().instrument(enable_content_recording=args.sensitive_data)

    with (
        tracer.start_as_current_span("Scenario: concurrent", kind=SpanKind.CLIENT) as root,
        identity.get_credential(args) as credential,
        AIProjectClient(endpoint=args.endpoint, credential=credential, allow_preview=True) as project,
        observability.agent_versions(project, observability.specs_for(*BRANCHES, AGGREGATOR),
                                     args.deployment, schemas) as agents,
        project.get_openai_client() as client,
    ):
        observability.announce(args, root)
        print(f"  branches {', '.join(BRANCHES)}, then {AGGREGATOR} aggregates")

        answers = asyncio.run(fan_out(client, agents, task, handlers, args.sensitive_data))
        for name, text in zip(BRANCHES, answers):
            print(f"\n  [{name}] {text}")

        # The barrier. Everything above has landed by the time this line runs, which is
        # what the aggregator's span start time proves in the trace.
        for name in BRANCHES:
            observability.handoff(name, AGGREGATOR, "fan-in: every branch has answered")

        sections = "\n\n".join(f"{name}:\n{text}" for name, text in zip(BRANCHES, answers))
        conversation = client.conversations.create()
        observability.record_state(conversation.id, len(sections))
        try:
            summary, _ = observability.run_turn(
                client, agents[AGGREGATOR], conversation.id,
                f"task: {task}\n\nThree agents answered it separately. Consolidate them.\n\n{sections}",
                handlers, args.sensitive_data)
            print(f"\n  [{AGGREGATOR}] {summary}")
        finally:
            client.conversations.delete(conversation_id=conversation.id)

    observability.report(args, capture)


if __name__ == "__main__":
    main()
