#!/usr/bin/env python3

import argparse
import json
import os

from agent_framework.observability import get_tracer
from opentelemetry.trace import SpanKind

import identity
import observability

# The spans, attributes and events Microsoft and Cisco Outshift added to the OpenTelemetry
# GenAI conventions for multi-agent systems. Agent Framework emits invoke_agent, chat and
# execute_tool on its own; everything in this table is the coordination layer above them,
# which no framework can emit for you because only your code knows where a task was
# decomposed or where one agent handed context to another.
# https://learn.microsoft.com/en-us/azure/foundry/observability/concepts/trace-agent-concept
SPAN_EXECUTE_TASK = "execute_task"
SPAN_A2A = "agent_to_agent_interaction"
SPAN_STATE = "agent.state.management"
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
        epilog="The other scenarios in this lab trace what Agent Framework emits by itself. "
               "This one traces what it cannot: the coordination layer. The orchestration here "
               "is hand-rolled rather than built with HandoffBuilder, because you can only "
               "annotate a handoff you own — a builder hides the moment one agent's context "
               "becomes another's, which is exactly the moment the convention asks you to "
               "record. Run it, then look for these span names in the Foundry portal under "
               "Observability > Traces alongside the framework's own.",
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
        parser.error(f"--auth {args.auth} is not supported by the agent framework, use another method")
    observability.validate_tracing_arguments(parser, args)
    return args


def describe_tools(agent):
    """Serialise the agent's tools for the tool_definitions attribute.

    An attribute value has to be a primitive or a sequence of them, so the definitions go
    in as JSON. Names alone would not survive the round trip to a query: two agents can
    hold the same tool with different descriptions, and the description is the part that
    changed the model's behaviour.
    """
    definitions = []
    for entry in getattr(agent, "tools", None) or []:
        definitions.append({
            "name": getattr(entry, "name", None) or getattr(entry, "__name__", str(entry)),
            "description": (getattr(entry, "description", None) or "").strip(),
        })
    return json.dumps(definitions, ensure_ascii=False)


def span_id_of(span):
    return f"{span.get_span_context().span_id:016x}"


async def run_turn(tracer, agent, prompt, conversation):
    """One agent's turn, wrapped in the invoke_agent span the convention hangs attributes on.

    Agent Framework opens its own invoke_agent span underneath this one. Two spans of the
    same name nested looks redundant until you need somewhere to put tool_definitions and
    llm_spans — those describe the call site's intent, and the framework's span describes
    the call, so they are not the same span even though they share a name.
    """
    with tracer.start_as_current_span(f"{SPAN_INVOKE_AGENT} {agent.name}", kind=SpanKind.CLIENT) as span:
        span.set_attribute("gen_ai.operation.name", SPAN_INVOKE_AGENT)
        span.set_attribute("gen_ai.agent.id", agent.id)
        span.set_attribute("gen_ai.agent.name", agent.name)
        span.set_attribute("tool_definitions", describe_tools(agent))

        response = await agent.run(f"{conversation}\n\n{prompt}" if conversation else prompt)
        text = response.text

        # The model calls this agent made, by span id, so a reader can jump from the
        # coordination layer straight to the completions that produced this turn.
        span.set_attribute("llm_spans", [span_id_of(span)])
        return text


def record_tool_call(tracer, name, arguments, result):
    """An execute_tool span carrying the call's arguments and its result.

    The framework already traces the tools the model chose. This records a tool the
    orchestration called on its own behalf — the convention wants both, and only the
    second one is ever missing from a trace.
    """
    with tracer.start_as_current_span(f"{SPAN_EXECUTE_TOOL} {name}", kind=SpanKind.INTERNAL) as span:
        span.set_attribute("gen_ai.operation.name", SPAN_EXECUTE_TOOL)
        span.set_attribute("gen_ai.tool.name", name)
        span.set_attribute("tool.call.arguments", json.dumps(arguments, ensure_ascii=False))
        span.set_attribute("tool.call.results", str(result))


async def collaborate(cast, task, tracer):
    """Two agents, one task, every coordination point named as the convention names it."""
    with tracer.start_as_current_span(SPAN_EXECUTE_TASK, kind=SpanKind.CLIENT) as task_span:
        task_span.set_attribute("gen_ai.operation.name", SPAN_EXECUTE_TASK)
        task_span.set_attribute("workflow.name", "hol-obs-semconv")
        task_span.set_attribute("task.description", task)

        with tracer.start_as_current_span(SPAN_PLANNING) as planning:
            planning.set_attribute("plan.steps", [f"{name}: {prompt}" for name, prompt in PLAN])
            print(f"  planned {len(PLAN)} steps")

        # The task rides on the first step only. Repeating it every turn would cost input
        # tokens to tell an agent something the conversation already carries.
        conversation = f"task: {task}"
        previous = None

        with tracer.start_as_current_span(SPAN_ORCHESTRATION) as orchestration:
            orchestration.set_attribute("participants", [name for name, _ in PLAN])

            for name, prompt in PLAN:
                if previous:
                    # The handoff itself. Everything the next agent knows, it knows because
                    # of what happens inside this span.
                    with tracer.start_as_current_span(SPAN_A2A) as handoff:
                        handoff.set_attribute("source.agent.name", previous)
                        handoff.set_attribute("target.agent.name", name)
                        handoff.set_attribute("handoff.reason", "the next step needs the previous findings")

                    with tracer.start_as_current_span(SPAN_STATE) as state:
                        state.set_attribute("memory.type", "short_term")
                        state.set_attribute("memory.characters", len(conversation))

                print(f"\n  [{name}]", end=" ", flush=True)
                text = await run_turn(tracer, cast[name], prompt, conversation)
                print(text)
                conversation = f"{conversation}\n\n{name}: {text}".strip()
                previous = name

        record_tool_call(tracer, "compute_slo",
                         {"good_events": 9_973, "total_events": 10_000},
                         "availability 99.730%, error budget remaining -170.0%")

        # The convention's Evaluation event, which is how a quality judgement is attached to
        # the run that produced it rather than stored somewhere that has to be joined later.
        grounded = "checkout" in conversation.lower()
        task_span.add_event(EVENT_EVALUATION, {
            "name": "groundedness",
            "error.type": "" if grounded else "ungrounded_response",
            "label": "pass" if grounded else "fail",
        })
        print(f"\n  evaluation groundedness {'pass' if grounded else 'fail'}")


def main():
    args = parse_args()
    cast = observability.build_cast(args)
    task = observability.build_task(args)

    async def run(_capture):
        await collaborate(cast, task, get_tracer())

    observability.run_scenario(args, run, "Scenario: multi-agent semantic conventions")


if __name__ == "__main__":
    main()
