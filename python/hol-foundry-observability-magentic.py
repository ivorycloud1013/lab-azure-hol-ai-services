#!/usr/bin/env python3

import argparse
import json
import os

from agent_framework import AgentResponseUpdate, Message
from agent_framework.orchestrations import MagenticBuilder, MagenticProgressLedger

import identity
import observability

PARTICIPANTS = ["researcher", "analyst"]

# The manager re-plans every round, so rounds are the cost knob for this scenario and
# these defaults are deliberately the smallest that still shows a plan and a re-plan.
MAX_ROUNDS = 3
MAX_STALLS = 1
MAX_RESETS = 1


def parse_args():
    parser = argparse.ArgumentParser(
        description="Trace a magentic multi-agent workflow on Foundry — a manager plans the "
                    "task, picks a specialist each round, tracks progress and re-plans when "
                    "the team stalls.",
        epilog="The most expensive pattern in this lab and the most interesting trace: the "
               "manager's planning and progress ledgers arrive as magentic_orchestrator events "
               "alongside the ordinary agent spans, so you can see the decision that produced "
               "each round rather than only its result. Rounds default to "
               f"{MAX_ROUNDS} because the manager calls the model on every one of them — raise "
               "--max-rounds knowingly. Compare with -groupchat, which coordinates without "
               "planning.",
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

    group = parser.add_argument_group("magentic")
    group.add_argument("--max-rounds", type=int, default=MAX_ROUNDS,
                       help="coordination rounds before the manager gives up")
    group.add_argument("--show-ledger", action="store_true",
                       help="print the plan and each progress ledger as the manager produces them")

    args = parser.parse_args()
    if not args.endpoint:
        parser.error("--endpoint or FOUNDRY_PROJECT_ENDPOINT is required")
    if args.auth in ("api-key", "access-token"):
        parser.error(f"--auth {args.auth} is not supported by the agent framework, use another method")
    if args.max_rounds < 1:
        parser.error("--max-rounds must be at least 1")
    observability.validate_tracing_arguments(parser, args)
    return args


def build_workflow(cast, args):
    participants = [cast[name] for name in PARTICIPANTS]
    # Plan review stays off. It pauses for a human, and nothing in a lab run answers.
    return MagenticBuilder(
        participants=participants,
        intermediate_output_from=participants,
        manager_agent=cast["manager"],
        max_round_count=args.max_rounds,
        max_stall_count=MAX_STALLS,
        max_reset_count=MAX_RESETS,
    ).build()


def print_orchestrator_event(event):
    """The manager's own thinking. Not a span — an event on the workflow stream — which is
    why it needs printing separately to line up with the span tree afterwards."""
    print(f"\n  [manager {event.data.event_type.name}]")
    content = event.data.content
    if isinstance(content, Message):
        print(f"    {content.text}")
    elif isinstance(content, MagenticProgressLedger):
        print(f"    {json.dumps(content.to_dict(), ensure_ascii=False, indent=4)}")


async def converse(workflow, task, show_ledger):
    """Stream the run, keeping participant text and manager events apart.

    observability.stream cannot be reused here — magentic emits a third kind of event that
    the other four patterns never produce, and dropping it would hide the planning.
    """
    last_author = None
    events = workflow.run(task, stream=True)
    async for event in events:
        if event.type in ("intermediate", "output") and isinstance(event.data, AgentResponseUpdate):
            author = event.data.author_name or event.executor_id
            if author != last_author:
                print(f"\n  [{author}]", end=" ", flush=True)
                last_author = author
            print(event.data.text or "", end="", flush=True)
        elif event.type == "magentic_orchestrator" and show_ledger:
            print_orchestrator_event(event)
            last_author = None
    print()


def main():
    args = parse_args()
    cast = observability.build_cast(args)
    task = observability.build_task(args)

    async def run(_capture):
        workflow = build_workflow(cast, args)
        print(f"  manager over {', '.join(PARTICIPANTS)}, at most {args.max_rounds} rounds")
        await converse(workflow, task, args.show_ledger)

    observability.run_scenario(args, run, "Scenario: magentic")


if __name__ == "__main__":
    main()
