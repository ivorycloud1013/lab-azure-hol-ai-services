#!/usr/bin/env python3
"""step 3 — artifact 레이어. 한 일을 두 번 하지 않도록 둘 자리.

step 2 는 대화를 작게 유지했다. 그건 이 문제와 다르다 — 대화가 가벼워져도 *한 일* 은 여전히
버려진다. 질문 세 개를 던지고 그 셋을 묶어 정리하라고 하면, 저장할 데가 없는 agent 는 10초 전에
찾은 것을 찾으러 문서로 되돌아간다.

해법은 멋있지 않다 — tool 두 개(save_note, read_note)와 파일이 놓일 디렉터리. 달라지는 것은
발견이 *이름을 가진 물건* 이 된다는 점이고, 그래서 나중에 다시 구하는 대신 가리킬 수 있게 된다.

돌려보고, --no-artifacts 로 다시 돌리세요. 볼 것은 마지막 질문입니다.
"""

import os
import re
import tempfile
import time

import harness_cli
import harness_loop
import harness_metrics as metrics
import step1_tools
from golden import is_hit
from harness_metrics import ToolResult

INSTRUCTIONS = (
    "당신은 한국 주택시장 보고서 하나에 대해 답합니다. "
    "답하기 전에 반드시 먼저 검색하세요. "
    "확인한 수치는 save_note 로 저장하고, 이미 기록해 둔 것은 다시 검색하지 말고 "
    "read_note 로 읽으세요. "
    "보고하는 모든 수치에 근거를 [line N] 형식으로 다세요. 질문한 언어로 답하세요."
)

INSTRUCTIONS_NO_STORE = step1_tools.INSTRUCTIONS

# note 이름은 모델이 정하고, 그게 파일시스템에 닿는다. 이 문자 집합을 벗어나면 거부한다 —
# 이름을 받아 경로를 여는 tool 은 agent 하네스가 파일 쓰기 수단으로 변하는 가장 흔한 길이다.
SAFE_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

MAX_NOTE_CHARS = 4000

# 저장소를 가질 만한 값어치를 만드는 질문. 앞선 발견 전부가 한 번에 필요해서, note 가 없는
# agent 는 그 전부를 문서에서 다시 찾아야 한다.
SUMMARY_QUESTION = "앞서 확인한 수치들을 한 표로 정리해 주세요. 각 항목에 근거 줄번호를 남기세요."

STORE_TOOLS = [
    {
        "type": "function",
        "name": "save_note",
        "description": "확인한 사실을 짧은 이름으로 저장한다. 나중에 문서를 다시 검색하지 않고 "
                       "읽어올 수 있다.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "짧은 식별자. 영문자·숫자·하이픈·밑줄만"},
                "content": {"type": "string",
                            "description": "저장할 내용. [line N] 근거를 포함할 것"},
            },
            "required": ["name", "content"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "read_note",
        "description": "save_note 로 앞서 저장해 둔 내용을 읽는다.",
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "저장할 때 쓴 이름"}},
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "list_notes",
        "description": "지금까지 저장한 내용의 이름을 모두 나열한다.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]

TOOLS = step1_tools.TOOLS + STORE_TOOLS


def note_path(directory, name):
    """평범한 이름이 아니면 경로가 되기 전에 거부한다.

    모델이 준 '../../x' 를 os.path.join 에 넣으면 디렉터리를 태연히 벗어난다. 이 검사는
    schema 가 아니라 여기 있어야 한다 — description 은 부탁이지 제약이 아니고, 이 필드에
    문자열을 넣을 수 있는 게 모델뿐인 것도 아니다.
    """
    if not SAFE_NAME.match(name or ""):
        return None
    return os.path.join(directory, f"{name}.md")


def dispatch(ctx, name, arguments):
    """저장소 tool 을 처리하고, 나머지는 step 1 이 지은 dispatcher 에게 넘긴다."""
    directory = ctx["artifact_dir"]
    try:
        if name == "save_note":
            path = note_path(directory, arguments["name"])
            if path is None:
                return ToolResult(f"note 이름 {arguments['name']!r} 이 잘못되었습니다. "
                                  "영문자·숫자·하이픈·밑줄만 쓰세요", False)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(arguments["content"][:MAX_NOTE_CHARS])
            return ToolResult(f"{arguments['name']} 저장함", True)

        if name == "read_note":
            path = note_path(directory, arguments["name"])
            if path is None or not os.path.isfile(path):
                existing = ", ".join(sorted(list_notes(directory))) or "없음"
                return ToolResult(f"{arguments['name']!r} 이라는 note 가 없습니다. "
                                  f"저장된 것: {existing}", False)
            with open(path, encoding="utf-8") as handle:
                return ToolResult(handle.read(), True)

        if name == "list_notes":
            names = sorted(list_notes(directory))
            return ToolResult(", ".join(names) if names else "아직 저장된 note 가 없습니다",
                              bool(names))
    except (KeyError, TypeError, ValueError, OSError) as error:
        return ToolResult(f"{name} 의 인자가 잘못되었습니다: {error}", False)

    return step1_tools.dispatch(ctx, name, arguments)


def list_notes(directory):
    return [f[:-3] for f in os.listdir(directory) if f.endswith(".md")]


def reuse_rate(run):
    """저장해 둔 것을 다시 검색하는 대신 읽어온 tool call 의 비율.

    저장소가 꺼져 있으면 None 이다. 0 으로 찍으면 재사용할 게 없었을 뿐인데 'agent 가 재사용을
    안 했다' 로 읽히기 때문이다.
    """
    calls = run["tool_calls"]
    reads = sum(1 for call in calls if call["name"] in ("read_note", "list_notes"))
    return reads / len(calls) if calls else None


def parse_args():
    parser = harness_cli.build_parser(
        description="step 3 — 발견이 그것을 만든 turn 보다 오래 살아남도록 artifact 레이어를 짓는다.",
        epilog="한 번 돌리고 --no-artifacts 로 다시 돌려, 마지막 질문의 redundant calls 를 비교하세요.",
    )
    parser.add_argument("--no-artifacts", dest="artifacts", action="store_false",
                        help="대조군: 검색 tool 만 주고 저장할 데를 주지 않는다")
    parser.add_argument("--artifact-dir", default=None, help="note 를 쓸 디렉터리")
    return harness_cli.finish_parsing(parser)


def main():
    args = parse_args()
    directory = args.artifact_dir or tempfile.mkdtemp(prefix="harness-artifacts-")
    os.makedirs(directory, exist_ok=True)
    ctx = {**harness_cli.prepare(args), "run": metrics.new_run(), "artifact_dir": directory}

    tools = TOOLS if args.artifacts else step1_tools.TOOLS
    router = dispatch if args.artifacts else step1_tools.dispatch
    instructions = INSTRUCTIONS if args.artifacts else INSTRUCTIONS_NO_STORE

    metrics.header("step 3 — artifact 저장" + ("" if args.artifacts else " (OFF — 대조군)"),
                   f"{args.deployment} · 질문 {len(ctx['golden'])}개, 그다음 그 전부가 필요한 질문 하나")

    started = time.perf_counter()
    previous_id = None
    hits = 0
    for item in ctx["golden"]:
        text, previous_id = harness_loop.run_turn(ctx, item["question"], tools, router,
                                                  instructions, previous_id)
        hit = is_hit(item, text)
        hits += hit
        print(f"  {'hit ' if hit else 'miss'} {item['id']}")

    before = len(ctx["run"]["tool_calls"])
    summary, _ = harness_loop.run_turn(ctx, SUMMARY_QUESTION, tools, router,
                                       instructions, previous_id)
    on_summary = len(ctx["run"]["tool_calls"]) - before
    print(f"  정리 질문에 tool call {on_summary}회 사용")

    rate = reuse_rate(ctx["run"])
    metrics.report("artifact 저장" + ("" if args.artifacts else " (꺼짐)"),
                   ctx["run"], time.perf_counter() - started, hits, len(ctx["golden"]),
                   extra={"artifact reuse": "n/a" if rate is None else f"{rate * 100:.1f}%",
                          "calls on summary": on_summary,
                          "notes written": len(list_notes(directory))})

    print(f"\n  note: {directory}")
    print("  redundant calls 는 앞선 호출과 완전히 같았던 호출 수입니다. 저장소가 없으면")
    print("  정리 질문이 모든 것을 두 번째로 찾아내야 합니다.")
    print("  다음: step 4 는 시작하기 전에 무엇을 할지 정하게 합니다.")


if __name__ == "__main__":
    main()
