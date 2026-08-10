#!/usr/bin/env python3

import argparse
import os

from agent_framework.orchestrations import ConcurrentBuilder

import identity
import observability

BRANCHES = ["researcher", "analyst", "reviewer"]


def parse_args():
    parser = argparse.ArgumentParser(
        description="Trace a concurrent multi-agent fan-out on Foundry — three agents answer "
                    "the same task in parallel and an aggregator merges the answers.",
        epilog="This is the scenario that produces the fan-in evidence: while the branches are "
               "still running, the aggregator's edge group reports edge_group.delivery_status "
               "'buffered', and only once the last branch lands does it flip to 'delivered'. "
               "Compare the wall time in the span tree against -sequential over the same task. "
               "hol-foundry-observability-verify.py asserts the buffered status from this "
               "scenario specifically, because no other pattern here produces it.",
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

    group = parser.add_argument_group("concurrent")
    group.add_argument("--summarize", action="store_true",
                       help="replace the default aggregator with one that asks the writer to "
                            "consolidate the branches — one more model call, one more agent span")

    args = parser.parse_args()
    if not args.endpoint:
        parser.error("--endpoint or FOUNDRY_PROJECT_ENDPOINT is required")
    if args.auth in ("api-key", "access-token"):
        parser.error(f"--auth {args.auth} is not supported by the agent framework, use another method")
    observability.validate_tracing_arguments(parser, args)
    return args


def build_aggregator(writer):
    """A custom aggregator runs after the barrier, so its span is the one that proves every
    branch had already landed."""

    async def summarize(results):
        sections = []
        for result in results:
            messages = getattr(result.agent_response, "messages", [])
            text = messages[-1].text if messages else "(no content)"
            sections.append(f"{result.executor_id}:\n{text}")
        response = await writer.run("\n\n".join(sections))
        return response.messages[-1].text if response.messages else ""

    return summarize


def build_workflow(cast, args):
    participants = [cast[name] for name in BRANCHES]
    # Branch output is intermediate so each agent's turn is visible while the run is still
    # going. Without it the terminal is silent until the aggregator finishes.
    builder = ConcurrentBuilder(participants=participants, intermediate_output_from=participants)
    if args.summarize:
        builder = builder.with_aggregator(build_aggregator(cast["writer"]))
    return builder.build()


def main():
    args = parse_args()
    cast = observability.build_cast(args)
    task = observability.build_task(args)

    async def run(_capture):
        workflow = build_workflow(cast, args)
        print(f"  branches {', '.join(BRANCHES)}"
              f"{' then writer aggregates' if args.summarize else ''}")
        await observability.stream(workflow, task)

    observability.run_scenario(args, run, "Scenario: concurrent")


if __name__ == "__main__":
    main()
