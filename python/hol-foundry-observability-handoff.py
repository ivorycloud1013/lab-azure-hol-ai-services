#!/usr/bin/env python3

import argparse
import os

from agent_framework.orchestrations import HandoffBuilder

import identity
import observability

PARTICIPANTS = ["researcher", "analyst", "writer"]

# Handoff is interactive by design — an agent that does not hand off gives control back
# to a human. Nobody is at the terminal in a lab run, so autonomous mode is not optional
# here, and the turn limit is what stops the agents talking to each other forever.
AUTONOMOUS_TURN_LIMIT = 3


def parse_args():
    parser = argparse.ArgumentParser(
        description="Trace a handoff multi-agent workflow on Foundry — each agent decides "
                    "which agent should take the conversation next.",
        epilog="Handoffs are tool calls, so the decision to transfer shows up in the trace as "
               "an execute_tool span on the agent that gave up control. That is the difference "
               "from -groupchat, where an orchestrator picks the speaker and no agent chooses. "
               "Autonomous mode is forced on because an interactive handoff would block on "
               "input that a lab run never provides. --add-handoff restricts the mesh so you "
               "can see routing change in the trace.",
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
    group.add_argument("--restrict-routes", action="store_true",
                       help="researcher may only reach analyst, analyst only writer — a chain "
                            "the agents still choose to walk, rather than a free mesh")
    group.add_argument("--turn-limit", type=int, default=AUTONOMOUS_TURN_LIMIT,
                       help="autonomous turns per agent before the workflow stops")

    args = parser.parse_args()
    if not args.endpoint:
        parser.error("--endpoint or FOUNDRY_PROJECT_ENDPOINT is required")
    if args.auth in ("api-key", "access-token"):
        parser.error(f"--auth {args.auth} is not supported by the agent framework, use another method")
    if args.turn_limit < 1:
        parser.error("--turn-limit must be at least 1")
    observability.validate_tracing_arguments(parser, args)
    return args


def build_workflow(cast, args):
    participants = [cast[name] for name in PARTICIPANTS]
    builder = HandoffBuilder(
        name="hol-obs-handoff",
        participants=participants,
    ).with_start_agent(cast["researcher"])

    if args.restrict_routes:
        builder = (builder
                   .add_handoff(cast["researcher"], [cast["analyst"]])
                   .add_handoff(cast["analyst"], [cast["writer"]]))

    limits = {name: args.turn_limit for name in PARTICIPANTS}
    return builder.with_autonomous_mode(turn_limits=limits).build()


def main():
    args = parse_args()
    cast = observability.build_cast(args)
    task = observability.build_task(args)

    async def run(_capture):
        workflow = build_workflow(cast, args)
        routes = "researcher -> analyst -> writer" if args.restrict_routes else "full mesh"
        print(f"  start researcher, routes {routes}, {args.turn_limit} autonomous turns each")
        await observability.stream(workflow, task)

    observability.run_scenario(args, run, "Scenario: handoff")


if __name__ == "__main__":
    main()
