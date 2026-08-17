"""문서를 검색하고 읽는 일 — 능력이지 하네스가 아니다.

이 파일이 만드는 경계가 랩 전체의 설계다. grep 과 sed 는 agent 가 *할 수 있는 일* 이고
모든 단계에서 똑같다. 그것을 모델에게 어떻게 설명하는 schema, 실제 코드로 보내는 dispatcher,
실패했을 때 무슨 일이 일어나는지 — 이것들이 *여러분이 짓는 것* 이고, 바뀌는 게 보이도록
각 단계 파일 안에 있다.

능력을 고정하는 것은 숫자에 의미를 주기 위해서이기도 하다. 어떤 단계가 왕복을 줄였다면
검색이 좋아진 게 아니다. 검색은 이 파일이고 바뀌지 않았으니 그럴 수가 없다. 남는 것은
하네스뿐이다.
"""

import subprocess

MAX_MATCHES = 40
CONTEXT_LINES = 2
MAX_CONTEXT_LINES = 10
MAX_PATTERNS = 8
MAX_READ_LINES = 200

# grep 은 "찾은 게 없음" 에 1 을 돌려주는데 그건 에러가 아니다. 그보다 큰 값이 에러다.
GREP_NO_MATCH_RETURNCODE = 1


def run_command(command):
    """shell 을 안 쓴다. 모델이 준 pattern 이 두 번째 명령으로 변할 수 없게."""
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode > GREP_NO_MATCH_RETURNCODE:
        return f"command failed: {result.stderr.strip()}"
    return result.stdout


def grep(path, patterns, context_lines=CONTEXT_LINES):
    """여러 pattern 을 한 번에 훑는다.

    pattern 여러 개로 한 번 부르는 것이 여러 번 나눠 부르는 것보다 낫다. 결과가 문서 순서대로
    섞여서 돌아오기 때문이다. 다만 agent 가 그걸 표현할 수 있느냐는 각 단계가 쓰는 schema 의
    문제이지 이 함수의 문제가 아니다.

    (text, matched) 를 돌려준다. 못 찾은 것은 실패가 아니다 — agent 는 제대로 된 질문을
    했고 사실인 답을 받았다. 그걸 어떻게 말해줄지는 각 단계가 정한다.
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
    """줄 범위를 읽는다. 상한을 두는 이유는, 파일 전체를 달라는 모델 하나가 context 관리
    문제를 다른 모든 단계로 도로 끌고 들어오기 때문이다."""
    start = max(int(start_line), 1)
    end = start + min(int(line_count), MAX_READ_LINES) - 1
    return run_command(["sed", "-n", f"{start},{end}p", path])
