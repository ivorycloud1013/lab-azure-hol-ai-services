#!/usr/bin/env python3

import argparse
import os

from agent_framework.orchestrations import GroupChatBuilder

import identity
import observability

PARTICIPANTS = ["researcher", "analyst", "writer", "reviewer"]

# Every assistant message is a model call, so this is the cost ceiling for the scenario.
MAX_ASSISTANT_MESSAGES = 6


def parse_args():
    parser = argparse.ArgumentParser(
        description="Trace a group-chat multi-agent workflow on Foundry — an orchestrator in "
                    "the middle decides who speaks next and when the conversation is done.",
        epilog="Star topology: the orchestrator sits between every pair of agents, so the span "
               "tree alternates orchestrator and participant instead of running agent to agent "
               "the way -handoff does. --orchestrator-agent swaps the round-robin selector for "
               "a real agent, which adds its own invoke_agent and chat spans on every round — "
               "the cheapest way to see what centralised coordination actually costs.",
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

    group = parser.add_argument_group("group chat")
    group.add_argument("--orchestrator-agent", action="store_true",
                       help="let an agent choose the speaker instead of round-robin")
    group.add_argument("--max-messages", type=int, default=MAX_ASSISTANT_MESSAGES,
                       help="stop after this many assistant messages")

    args = parser.parse_args()
    if not args.endpoint:
        parser.error("--endpoint or FOUNDRY_PROJECT_ENDPOINT is required")
    if args.auth in ("api-key", "access-token"):
        parser.error(f"--auth {args.auth} is not supported by the agent framework, use another method")
    if args.max_messages < 1:
        parser.error("--max-messages must be at least 1")
    observability.validate_tracing_arguments(parser, args)
    return args


def round_robin(state):
    """Pick the next speaker by round number. Deterministic on purpose — a fixed rotation
    makes two runs comparable when you are diffing traces rather than judging answers."""
    names = list(state.participants.keys())
    return names[state.current_round % len(names)]


def build_workflow(cast, args):
    participants = [cast[name] for name in PARTICIPANTS]
    # A hard message cap regardless of who is selecting. An agent orchestrator usually
    # stops earlier on its own, but "usually" is not a budget.
    options = {
        "participants": participants,
        "intermediate_output_from": participants,
        "termination_condition": lambda messages: sum(
            1 for message in messages if message.role == "assistant") >= args.max_messages,
    }
    if args.orchestrator_agent:
        options["orchestrator_agent"] = cast["manager"]
    else:
        options["selection_func"] = round_robin
    return GroupChatBuilder(**options).build()


def main():
    args = parse_args()
    cast = observability.build_cast(args)
    task = observability.build_task(args)

    async def run(_capture):
        workflow = build_workflow(cast, args)
        selector = "manager agent" if args.orchestrator_agent else "round robin"
        print(f"  {selector} over {', '.join(PARTICIPANTS)}, "
              f"stopping at {args.max_messages} assistant messages")
        await observability.stream(workflow, task)

    observability.run_scenario(args, run, "Scenario: group chat")


if __name__ == "__main__":
    main()
