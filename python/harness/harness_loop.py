"""The tool-calling loop, exactly as step 1 finishes building it.

Steps 2 through 5 import it rather than rebuild it. That is the whole reason this file
exists: once you have built the loop once you should not have to build it again, and a
later step that re-taught it would bury its own layer under boilerplate the learner
already wrote.

Read step1_tools.py first. Everything here was assembled there, one piece at a time.
"""

import json

import harness_metrics as metrics

# Ceiling on how many times the model may come back for more tools before we stop it.
# Without a ceiling a confused agent loops until the budget or the patience runs out;
# this is the cheapest guardrail in the whole lab and the one people forget.
MAX_TOOL_ROUNDS = 8


def collect_tool_calls(ctx, response, dispatch):
    """Run every tool the model asked for. Returns ([], run) when it asked for none.

    The outputs go back as function_call_output items keyed by call_id. Dropping one
    would leave the model waiting on an answer it never gets, and the next request fails
    rather than the loop simply ending.
    """
    run = ctx["run"]
    outputs = []
    for item in response.output:
        if item.type != "function_call":
            continue
        arguments = json.loads(item.arguments)
        result = dispatch(ctx, item.name, arguments)
        run = metrics.record_tool_call(run, item.name, arguments, result)
        if ctx["args"].show_tools:
            marker = "  " if result.ok else "! "
            print(f"    {marker}{item.name} {json.dumps(arguments, ensure_ascii=False)}")
        outputs.append({
            "type": "function_call_output",
            "call_id": item.call_id,
            "output": result.text,
        })
    return outputs, run


def run_turn(ctx, prompt, tools, dispatch, instructions=None, previous_id=None):
    """One prompt, driven to a final answer, with the cost folded into ctx['run'].

    ctx carries the run so a caller can put several turns into one measurement — which
    is what step 2 does for context strategies and step 4 does for plan execution.
    """
    args = ctx["args"]
    request = {"model": args.deployment, "input": prompt}
    if previous_id:
        # previous_response_id carries the history server-side, so only the new turn
        # goes up. This is also why the token count stops being obvious — see step 2.
        request["previous_response_id"] = previous_id
    elif instructions:
        request["instructions"] = instructions
    if tools:
        request["tools"] = tools

    response = ctx["client"].responses.create(**request)
    ctx["run"] = metrics.add_usage(ctx["run"], response.usage)

    for _ in range(MAX_TOOL_ROUNDS):
        if not tools:
            break
        outputs, run = collect_tool_calls(ctx, response, dispatch)
        ctx["run"] = run
        if not outputs:
            break
        response = ctx["client"].responses.create(
            model=args.deployment, previous_response_id=response.id,
            input=outputs, tools=tools)
        ctx["run"] = metrics.add_usage(ctx["run"], response.usage)
    else:
        # Out of rounds with calls still pending. Not fatal: an agent that flails is a
        # measurement, and raising here would throw away every question already paid for.
        print(f"    [gave up after {MAX_TOOL_ROUNDS} tool rounds]")

    ctx["run"] = {**ctx["run"], "text": response.output_text, "response_id": response.id}
    return response.output_text, response.id
