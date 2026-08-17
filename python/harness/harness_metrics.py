"""Shared instrumentation for the harness lab. Every step reports the same block.

If each step printed its own shape, a number could not be followed from one step to the
next — and following one number across steps is the only way a layer shows what it did.
So the format is fixed here and no step is allowed its own.

The one thing worth noticing in this file: a tool result carries whether it failed
(ToolResult.ok) instead of leaving that to be guessed from the text. You cannot report
tool_error_rate if failure is only a string that happens to start with "no match" —
making failure a first-class return value is itself part of building a harness.
"""

from collections import namedtuple

BANNER = "=" * 72
RULE = "-" * 72

# text goes back to the model; ok is for the metrics and never reaches the model.
ToolResult = namedtuple("ToolResult", "text ok")


def new_run():
    """The counters every step accumulates.

    turns counts model calls, not conversational turns — that is what the token columns
    divide by, and mixing the two makes tokens_per_turn quietly meaningless.
    """
    return {
        "text": "",
        "response_id": None,
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_tokens": 0,
        "turns": 0,
        "turn_tokens": [],   # input_tokens per model call, in order — the growth curve
        "tool_calls": [],    # {"name", "arguments", "ok"} per call, in order
    }


def add_usage(run, usage):
    """Fold one response's usage into a new run.

    cached_tokens is nested and missing on deployments that do not cache, so it is read
    defensively. A missing attribute here would end a run over the least important
    number on the board.
    """
    details = getattr(usage, "input_tokens_details", None)
    cached = getattr(details, "cached_tokens", 0) or 0 if details is not None else 0
    tokens = usage.input_tokens or 0
    return {
        **run,
        "input_tokens": run["input_tokens"] + tokens,
        "output_tokens": run["output_tokens"] + (usage.output_tokens or 0),
        "cached_tokens": run["cached_tokens"] + cached,
        "turns": run["turns"] + 1,
        "turn_tokens": run["turn_tokens"] + [tokens],
    }


def record_tool_call(run, name, arguments, result):
    """Log one tool call. Arguments are kept so redundant_work can spot repeats."""
    entry = {"name": name, "arguments": arguments, "ok": result.ok}
    return {**run, "tool_calls": run["tool_calls"] + [entry]}


def merge(first, second):
    """Add two runs together, keeping the later text and response id.

    Steps that ask several questions need one run per question and one total. Summing
    them here rather than in each step is what keeps the report identical everywhere.
    """
    return {
        "text": second["text"] or first["text"],
        "response_id": second["response_id"] or first["response_id"],
        "input_tokens": first["input_tokens"] + second["input_tokens"],
        "output_tokens": first["output_tokens"] + second["output_tokens"],
        "cached_tokens": first["cached_tokens"] + second["cached_tokens"],
        "turns": first["turns"] + second["turns"],
        "turn_tokens": first["turn_tokens"] + second["turn_tokens"],
        "tool_calls": first["tool_calls"] + second["tool_calls"],
    }


def error_rate(run):
    """Share of tool calls the dispatcher rejected. None when nothing was called."""
    calls = run["tool_calls"]
    if not calls:
        return None
    return sum(1 for call in calls if not call["ok"]) / len(calls)


def redundant_work(run):
    """How many tool calls repeated an earlier call exactly.

    A repeat is the signature of an agent that did not learn anything from the first
    result — either the tool told it nothing useful, or nothing kept the answer around.
    Steps 1 and 3 both move this number, for those two different reasons.
    """
    seen, repeats = set(), 0
    for call in run["tool_calls"]:
        key = (call["name"], repr(sorted(call["arguments"].items())))
        if key in seen:
            repeats += 1
        seen.add(key)
    return repeats


def growth_slope(turn_tokens):
    """Least-squares slope of input tokens against call number, in tokens per call.

    The headline number for context management. A conversation that carries its whole
    history climbs; one that compacts or retrieves flattens. Reporting the slope rather
    than the total is what makes two runs of different lengths comparable.
    """
    count = len(turn_tokens)
    if count < 2:
        return None
    mean_x = (count - 1) / 2
    mean_y = sum(turn_tokens) / count
    variance = sum((x - mean_x) ** 2 for x in range(count))
    if not variance:
        return None
    covariance = sum((x - mean_x) * (y - mean_y) for x, y in enumerate(turn_tokens))
    return covariance / variance


def cache_rate(run):
    return run["cached_tokens"] / run["input_tokens"] if run["input_tokens"] else None


def _line(label, value):
    print(f"  {label:<18}{value}")


def _ratio(value):
    return "n/a" if value is None else f"{value * 100:.1f}%"


def report(layer, run, seconds, hits=None, total=None, extra=None):
    """The block every step prints. Same labels, same order, every time."""
    print(f"\n{RULE}")
    _line("layer", layer)
    if total:
        _line("hit", f"{hits}/{total}")
    _line("turns", run["turns"])

    calls = run["tool_calls"]
    if calls:
        failures = sum(1 for call in calls if not call["ok"])
        _line("tool calls", f"{len(calls)}  (errors {failures}, {_ratio(error_rate(run))})")
        _line("redundant calls", redundant_work(run))
    else:
        _line("tool calls", "0")

    _line("input tokens", f"{run['input_tokens']:,}  (cached {_ratio(cache_rate(run))})")
    _line("output tokens", f"{run['output_tokens']:,}")
    if run["turns"]:
        _line("tokens/turn", f"{run['input_tokens'] // run['turns']:,}")
    slope = growth_slope(run["turn_tokens"])
    if slope is not None:
        _line("context growth", f"{slope:+,.0f} tokens/call")
    _line("seconds", f"{seconds:.1f}")

    for label, value in (extra or {}).items():
        _line(label, value)
    print(RULE)


def header(title, subtitle=None):
    print(f"\n{BANNER}")
    print(title)
    if subtitle:
        print(subtitle)
    print(BANNER)
