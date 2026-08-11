#!/usr/bin/env python3

import argparse
import os

from agent_framework.orchestrations import SequentialBuilder

import identity
import observability

PIPELINE = ["researcher", "analyst", "writer"]


def parse_args():
    parser = argparse.ArgumentParser(
        description="Trace a sequential multi-agent pipeline on Foundry — researcher, then "
                    "analyst, then writer, each building on the last.",
        epilog="The simplest shape to read in a trace, and the one to start from. Every "
               "executor.process span sits in the order the agents ran, so the span tree "
               "and the pipeline are the same picture. --intermediate also surfaces the "
               "agents before the last one as intermediate events. "
               "Sibling scenarios: -concurrent (fan-out), -handoff (agents choose the next "
               "agent), -groupchat (an orchestrator chooses), -magentic (a manager plans). "
               "hol-foundry-observability-verify.py runs all of them and asserts what each "
               "should have emitted.",
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

    group = parser.add_argument_group("sequential")
    group.add_argument("--intermediate", action="store_true",
                       help="surface every agent's turn, not just the last one's")
    group.add_argument("--chain-responses-only", action="store_true",
                       help="pass each agent only the previous agent's reply instead of the "
                            "whole conversation — fewer input tokens, less context")

    args = parser.parse_args()
    if not args.endpoint:
        parser.error("--endpoint or FOUNDRY_PROJECT_ENDPOINT is required")
    if args.auth in ("api-key", "access-token"):
        parser.error(f"--auth {args.auth} is not supported by the agent framework, use another method")
    observability.validate_tracing_arguments(parser, args)
    return args


def build_workflow(cast, args):
    """Participants in list order — that order is the pipeline, and it is also the order
    the executor.process spans will appear in.

    intermediate_output_from is passed only when asked for. Handing it None is not the
    same as leaving it out, and leaving it out is what selects the builder's default.
    """
    participants = [cast[name] for name in PIPELINE]
    options = {"chain_only_agent_responses": args.chain_responses_only}
    if args.intermediate:
        options["intermediate_output_from"] = participants[:-1]
    return SequentialBuilder(participants=participants, **options).build()


def main():
    args = parse_args()
    cast = observability.build_cast(args)
    task = observability.build_task(args)

    async def run(_capture):
        workflow = build_workflow(cast, args)
        print(f"  pipeline {' -> '.join(PIPELINE)}")
        await observability.stream(workflow, task)

    observability.run_scenario(args, run, "Scenario: sequential", cast)


if __name__ == "__main__":
    main()
