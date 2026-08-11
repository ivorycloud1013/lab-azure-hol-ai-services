#!/usr/bin/env python3

import argparse
import json
import os

from azure.ai.projects import AIProjectClient
from azure.ai.projects.telemetry import AIProjectInstrumentor
from opentelemetry.trace import SpanKind

import identity
import observability

# The spans, attributes and events Microsoft and Cisco Outshift added to the OpenTelemetry
# GenAI conventions for multi-agent systems. AIProjectInstrumentor emits create_agent and
# invoke_agent on its own; everything in this table is the coordination layer above them,
# which no SDK can emit for you because only your code knows where a task was decomposed
# or where one agent handed context to another.
# https://learn.microsoft.com/en-us/azure/foundry/observability/concepts/trace-agent-concept
SPAN_EXECUTE_TASK = "execute_task"
SPAN_PLANNING = "agent_planning"
SPAN_ORCHESTRATION = "agent_orchestration"
SPAN_INVOKE_AGENT = "invoke_agent"
SPAN_EXECUTE_TOOL = "execute_tool"
EVENT_EVALUATION = "Evaluation"

# Two turns is enough to have a real handoff to describe, and every extra turn is another
# model call for a script whose point is the annotation, not the answer.
PLAN = [
    ("researcher", "Collect the checkout metrics and today's incidents. Numbers only."),
    ("analyst", "Using the findings above, name the likely cause and judge the error budget."),
]


def parse_args():
    parser = argparse.ArgumentParser(
        description="Emit the multi-agent semantic conventions over a real two-agent "
                    "collaboration on Foundry — execute_task, agent_planning, "
                    "agent_orchestration, agent_to_agent_interaction, agent.state.management, "
                    "and the Evaluation event.",
        epilog="The other scenarios trace what the SDK emits by itself and add the handful of "
               "coordination spans their pattern needs. This one exists to show the whole "
               "vocabulary in one trace, including the two spans the others have no use for: "
               "agent_planning, which records the decomposition before any agent runs, and "
               "agent_orchestration, which is the bracket around the turns that carries out "
               "the plan. Run it with --trace-export azure-monitor — none of these spans are "
               "the service's, so without that flag none of them leave the machine — then "
               "look for the names in the Foundry portal under Agents > Traces alongside the "
               "SDK's own.",
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


def describe_tools(spec, schemas):
    """Serialise an agent's tools for the tool_definitions attribute.

    An attribute value has to be a primitive or a sequence of them, so the definitions go
    in as JSON. Names alone would not survive the round trip to a query: two agents can
    hold the same tool with different descriptions, and the description is the part that
    changed the model's behaviour.
    """
    return json.dumps([{"name": name, "description": schemas[name]["description"]}
                       for name in spec["tools"]], ensure_ascii=False)


def annotated_turn(tracer, client, agent, spec, schemas, conversation_id, prompt, handlers, sensitive):
    """One agent's turn, wrapped in the invoke_agent span the convention hangs attributes on.

    The instrumentor opens its own invoke_agent span underneath this one. Two spans of the
    same name nested looks redundant until you need somewhere to put tool_definitions and
    llm_spans — those describe the call site's intent, and the SDK's span describes the
    call, so they are not the same span even though they share a name.
    """
    with tracer.start_as_current_span(f"{SPAN_INVOKE_AGENT} {spec['name']}",
                                      kind=SpanKind.CLIENT) as span:
        span.set_attribute("gen_ai.operation.name", SPAN_INVOKE_AGENT)
        span.set_attribute("gen_ai.agent.id", agent.id)
        span.set_attribute("gen_ai.agent.name", agent.name)
        span.set_attribute("tool_definitions", describe_tools(spec, schemas))

        text, _ = observability.run_turn(client, agent, conversation_id, prompt,
                                         handlers, sensitive)

        # The model calls this turn made, by span id, so a reader can jump from the
        # coordination layer straight to the completions that produced it.
        span.set_attribute("llm_spans", [f"{span.get_span_context().span_id:016x}"])
        return text


def record_tool_call(tracer, name, arguments, result):
    """An execute_tool span carrying the call's arguments and its result.

    The other scenarios trace the tools the model chose, and they redact the arguments
    unless --sensitive-data is set, because those carry whatever the user asked. This
    records a tool the orchestration called on its own behalf, on numbers this file made
    up, so there is nothing to redact and the convention gets to be shown in full.
    """
    with tracer.start_as_current_span(f"{SPAN_EXECUTE_TOOL} {name}", kind=SpanKind.INTERNAL) as span:
        span.set_attribute("gen_ai.operation.name", SPAN_EXECUTE_TOOL)
        span.set_attribute("gen_ai.tool.name", name)
        span.set_attribute("tool.call.arguments", json.dumps(arguments, ensure_ascii=False))
        span.set_attribute("tool.call.results", str(result))


def main():
    args = parse_args()
    task = observability.build_task(args)
    schemas, handlers = observability.build_tools(args.inject_error)
    specs = {spec["name"]: spec for spec in observability.specs_for(*[name for name, _ in PLAN])}

    capture = observability.configure_providers(args)
    tracer = observability.get_tracer()

    os.environ["AZURE_EXPERIMENTAL_ENABLE_GENAI_TRACING"] = "true"
    AIProjectInstrumentor().instrument(enable_content_recording=args.sensitive_data)

    with (
        tracer.start_as_current_span("Scenario: multi-agent semantic conventions",
                                     kind=SpanKind.CLIENT) as root,
        identity.get_credential(args) as credential,
        AIProjectClient(endpoint=args.endpoint, credential=credential, allow_preview=True) as project,
        observability.agent_versions(project, list(specs.values()), args.deployment, schemas) as agents,
        project.get_openai_client() as client,
    ):
        observability.announce(args, root)

        with tracer.start_as_current_span(SPAN_EXECUTE_TASK, kind=SpanKind.CLIENT) as task_span:
            task_span.set_attribute("gen_ai.operation.name", SPAN_EXECUTE_TASK)
            task_span.set_attribute("workflow.name", "hol-obs-semconv")
            task_span.set_attribute("task.description", task)

            with tracer.start_as_current_span(SPAN_PLANNING) as planning:
                planning.set_attribute("plan.steps", [f"{name}: {prompt}" for name, prompt in PLAN])
                print(f"  planned {len(PLAN)} steps")

            conversation = client.conversations.create()
            transcript = ""
            previous = None

            try:
                with tracer.start_as_current_span(SPAN_ORCHESTRATION) as orchestration:
                    orchestration.set_attribute("participants", [name for name, _ in PLAN])

                    # The task rides on the first step only. Repeating it every turn would
                    # cost input tokens to tell an agent what the conversation already holds.
                    prompt_prefix = f"task: {task}\n\n"
                    for name, prompt in PLAN:
                        if previous:
                            observability.handoff(
                                previous, name, "the next step needs the previous findings")
                            observability.record_state(conversation.id, len(transcript))
                            prompt_prefix = ""

                        print(f"\n  [{name}]", end=" ", flush=True)
                        text = annotated_turn(tracer, client, agents[name], specs[name], schemas,
                                              conversation.id, prompt_prefix + prompt,
                                              handlers, args.sensitive_data)
                        print(text)
                        transcript += text
                        previous = name
            finally:
                client.conversations.delete(conversation_id=conversation.id)

            record_tool_call(tracer, "compute_slo",
                             {"good_events": 9_973, "total_events": 10_000},
                             "availability 99.730%, error budget remaining -170.0%")

            # The convention's Evaluation event, which is how a quality judgement is attached
            # to the run that produced it rather than stored somewhere that has to be joined.
            grounded = "checkout" in transcript.lower()
            task_span.add_event(EVENT_EVALUATION, {
                "name": "groundedness",
                "error.type": "" if grounded else "ungrounded_response",
                "label": "pass" if grounded else "fail",
            })
            print(f"\n  evaluation groundedness {'pass' if grounded else 'fail'}")

    observability.report(args, capture)


if __name__ == "__main__":
    main()
