#!/usr/bin/env python3

import argparse
import os

from azure.ai.projects import AIProjectClient
from azure.ai.projects.telemetry import AIProjectInstrumentor
from opentelemetry.trace import SpanKind

import identity
import observability

PARTICIPANTS = ["researcher", "analyst", "writer"]
START = "researcher"
HANDOFF_TOOL = "handoff_to"

# Handoff is open-ended by design, so something has to stop it. Four turns is enough for
# researcher to analyst to writer with one turn to spare, and every turn is a model call.
DEFAULT_TURN_LIMIT = 4


def parse_args():
    parser = argparse.ArgumentParser(
        description="Trace a handoff multi-agent workflow on Foundry — each agent decides "
                    "which agent should take the conversation next.",
        epilog="A handoff is a tool call and nothing more, which this file shows rather than "
               "claims: every agent holds a handoff_to tool, the loop reads whichever call the "
               "model made and gives the next turn to the agent it named. So the decision to "
               "transfer appears in the trace twice — as an execute_tool span on the agent that "
               "gave up control, and as the agent_to_agent_interaction span this file opens "
               "with the reason the model itself supplied. That is the difference from "
               "-sequential, where the order is in the source and no agent chooses. The "
               "handoff_to call is answered like any other tool: leaving a function call "
               "unanswered in a conversation is how the next agent's request gets rejected. "
               "Only one of those two spans is the service's, so the portal shows the handoff "
               "as a decision rather than a tool call unless the run passes "
               "--trace-export azure-monitor.",
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

    group = parser.add_argument_group("handoff")
    group.add_argument("--turn-limit", type=int, default=DEFAULT_TURN_LIMIT,
                       help="turns before the run stops, however the agents route it")

    args = parser.parse_args()
    if not args.endpoint:
        parser.error("--endpoint or FOUNDRY_PROJECT_ENDPOINT is required")
    if args.auth in ("api-key", "access-token"):
        parser.error(f"--auth {args.auth} is not supported by this SDK path, use another method")
    if args.turn_limit < 1:
        parser.error("--turn-limit must be at least 1")
    observability.validate_tracing_arguments(parser, args)
    return args


def build_handoff_tool(specs):
    """The tool that moves control, and the handler that answers it.

    The targets are an enum rather than a free string so the model cannot route to an agent
    that does not exist, and each one carries its description — an agent choosing the next
    agent needs to know what the others are for, and this is the only place it is told.

    The handler returns an acknowledgement and nothing else. Where control actually goes is
    decided by the loop below, from the arguments run_turn hands back; a handler that tried
    to move control itself would be a side effect hidden inside a tool call.
    """
    schema = {
        "type": "function",
        "name": HANDOFF_TOOL,
        "description": "Give the rest of the task to another agent. Call this instead of "
                       "answering when another agent is better placed to continue.",
        "parameters": {
            "type": "object",
            "properties": {
                "agent": {
                    "type": "string",
                    "enum": [spec["name"] for spec in specs],
                    "description": " / ".join(f"{spec['name']}: {spec['description']}"
                                              for spec in specs),
                },
                "reason": {"type": "string", "description": "why that agent should take it"},
            },
            "required": ["agent", "reason"],
            "additionalProperties": False,
        },
    }

    def handoff_to(agent, reason):
        return f"control passed to {agent}: {reason}"

    return {HANDOFF_TOOL: schema}, {HANDOFF_TOOL: handoff_to}


def main():
    args = parse_args()
    task = observability.build_task(args)
    schemas, handlers = observability.build_tools(args.inject_error)

    # Every participant gets the handoff tool on top of whatever it already holds. Tools
    # live on the agent definition — the service rejects a responses.create that carries
    # both an agent reference and a tools list — so this has to happen before creation.
    specs = observability.specs_for(*PARTICIPANTS)
    handoff_schemas, handoff_handlers = build_handoff_tool(specs)
    specs = [{**spec, "tools": [*spec["tools"], HANDOFF_TOOL]} for spec in specs]
    schemas = {**schemas, **handoff_schemas}
    handlers = {**handlers, **handoff_handlers}

    capture = observability.configure_providers(args)
    tracer = observability.get_tracer()

    os.environ["AZURE_EXPERIMENTAL_ENABLE_GENAI_TRACING"] = "true"
    AIProjectInstrumentor().instrument(enable_content_recording=args.sensitive_data)

    with (
        tracer.start_as_current_span("Scenario: handoff", kind=SpanKind.CLIENT) as root,
        identity.get_credential(args) as credential,
        AIProjectClient(endpoint=args.endpoint, credential=credential, allow_preview=True) as project,
        observability.agent_versions(project, specs, args.deployment, schemas, args.keep) as agents,
        project.get_openai_client() as client,
    ):
        observability.announce(args, root)
        print(f"  start {START}, at most {args.turn_limit} turns, routing chosen by the agents")

        with observability.conversation(client, args.keep) as conversation:
            current = START
            prompt = (f"task: {task}\n\nAnswer what you can. When another agent should take "
                      f"over, call {HANDOFF_TOOL} instead of guessing.")
            transcript = ""

            for turn in range(args.turn_limit):
                text, control = observability.run_turn(client, agents[current], conversation.id,
                                                       prompt, handlers, args.sensitive_data,
                                                       stop_on=HANDOFF_TOOL)
                print(f"\n  [{current}] {text}")
                transcript += text

                if not control:
                    print(f"\n  {current} answered without handing off, stopping after "
                          f"{turn + 1} turns")
                    break

                observability.handoff(current, control["agent"], control["reason"])
                observability.record_state(conversation.id, len(transcript))
                print(f"  -> {control['agent']}: {control['reason']}")

                current = control["agent"]
                prompt = "You have been handed the task. Continue it."
            else:
                print(f"\n  stopped at the {args.turn_limit} turn limit")

    observability.report(args, capture)


if __name__ == "__main__":
    main()
