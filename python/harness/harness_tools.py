"""Searching and reading the document — the capability, not the harness.

The split this file makes is the point of the whole lab. grep and sed are what the agent
can *do*, and they are identical in every step. The schema that describes them, the
dispatcher that routes to them, and what happens when they fail are what you *build*,
and those live in each step file where you can see them change.

Keeping the capability constant is also what makes the numbers mean anything. When a
step reports fewer rounds, the search did not get better — it could not have, it is this
file — so the harness is the only thing left to credit.
"""

import subprocess

MAX_MATCHES = 40
CONTEXT_LINES = 2
MAX_CONTEXT_LINES = 10
MAX_PATTERNS = 8
MAX_READ_LINES = 200

# grep exits 1 for "no match", which is not an error. Anything above it is.
GREP_NO_MATCH_RETURNCODE = 1


def run_command(command):
    """No shell, so a pattern from the model cannot turn into a second command."""
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode > GREP_NO_MATCH_RETURNCODE:
        return f"command failed: {result.stderr.strip()}"
    return result.stdout


def grep(path, patterns, context_lines=CONTEXT_LINES):
    """Match any of the patterns in one pass.

    One call with several patterns beats several calls, because the hits come back
    interleaved in document order. Whether the agent can express that is a property of
    the schema a step writes, not of this function.

    Returns (text, matched). A miss is not a failure — the agent asked a well-formed
    question and got a true answer. Steps decide how to say that.
    """
    kept = [p for p in patterns if p][:MAX_PATTERNS]
    if not kept:
        return "no pattern given", False
    command = ["grep", "--line-number", "--ignore-case", "--extended-regexp",
               f"--context={max(0, min(context_lines, MAX_CONTEXT_LINES))}"]
    for pattern in kept:
        command += ["-e", pattern]

    output = run_command(command + ["--", path])
    lines = output.splitlines()
    if not lines:
        return f"no match for {' | '.join(kept)}", False
    if len(lines) > MAX_MATCHES:
        return "\n".join(lines[:MAX_MATCHES]) + (
            f"\n… {len(lines) - MAX_MATCHES} more lines, narrow the patterns "
            "or lower context_lines"), True
    return output, True


def read(path, start_line, line_count):
    """Read a line range. Clamped, because a model that asks for the whole file would
    put the context-management lesson back into every other step."""
    start = max(int(start_line), 1)
    end = start + min(int(line_count), MAX_READ_LINES) - 1
    return run_command(["sed", "-n", f"{start},{end}p", path])
