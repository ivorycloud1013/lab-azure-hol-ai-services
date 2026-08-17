# Foundry Harness — 하네스를 직접 쌓는 랩

모델을 부르는 코드는 다들 한 번쯤 써봤습니다. 그 코드가 **에이전트**가 되려면 주위에
무엇이 더 있어야 하는가 — 이 랩은 그걸 한 층씩 직접 지으면서 답합니다.

## 하네스란

**모델을 둘러싼 실행 레이어**입니다. 다섯 가지로 나눕니다.

| 레이어 | 하는 일 | 없으면 |
|---|---|---|
| **툴 호출** | 모델이 바깥과 무언가를 할 수 있게 한다 | 아는 것만 말한다 |
| **컨텍스트 관리** | 턴 사이에 무엇을 가지고 갈지 정한다 | 다 들고 가다 비용이 터지거나, 다 버리고 잊는다 |
| **아티팩트 저장** | 알아낸 것을 이름 붙여 남긴다 | 방금 찾은 걸 또 찾는다 |
| **플래닝** | 움직이기 전에 경로를 정한다 | 반응만 하다 헤맨다 |
| **평가** | 바꾼 게 나아졌는지 판정한다 | 고친 줄 알고 망가뜨린다 |

이 랩의 단계가 곧 이 다섯 레이어입니다. **한 단계에 한 레이어를 짓고, 그 레이어가 움직이는
숫자를 같은 형식으로 찍습니다.** 숫자가 왜 움직였는지 헷갈리지 않도록, 검색 능력 자체
(`harness_tools.py` 의 grep · sed)는 **전 단계에서 한 글자도 바뀌지 않습니다.**

## 최적화는 어느 층에서 하는가

같은 레이어라도 손댈 수 있는 층이 여럿입니다. 아래로 갈수록 영향 범위가 넓고 되돌리기 어렵습니다.

```
프롬프트  →  구조화된 컨텍스트  →  워크플로  →  하네스 코드  →  옵티마이저 코드
```

툴 호출 레이어를 예로 들면:

| 층 | 여기서는 그게 뭔가 |
|---|---|
| 프롬프트 | "먼저 검색하라"는 지시문 한 줄 |
| 구조화된 컨텍스트 | 툴 스키마의 description · 인자 이름 · 예시 |
| 워크플로 | 검색 → 읽기 → 답변이라는 고정된 순서 |
| 하네스 코드 | 디스패처가 실패를 어떤 문장으로 돌려주는가 |
| 옵티마이저 코드 | 실패 로그를 모아 description 을 자동으로 고쳐 쓰는 루프 |

**마지막 칸은 이 랩에서 짓지 않습니다.** 다만 사다리에 그 칸이 있다는 것 자체가 알아둘
내용입니다 — 하네스를 손으로 튜닝하는 단계 위에, 하네스를 고치는 코드를 짓는 단계가 있습니다.

> 이 레이어 구분과 최적화 사다리는 2026년 상반기에 자리 잡은 프레이밍을 따랐습니다.
> 원문 출처는 확인하지 못해 링크를 달지 않았습니다.

## 진행 순서

```bash
cd python
pip install -r requirements.txt
export AZURE_AI_PROJECT_ENDPOINT="<foundry-project-endpoint>"
```

| 단계 | 파일 | 짓는 것 | 보는 숫자 |
|---|---|---|---|
| 0 | `step0_baseline.py` | (아무것도 없음) | baseline. `hit` 이 바닥 |
| 1 | `step1_tools.py` | 스키마 · 디스패처 · 루프 · 실패 처리 | `tool error rate`, `hit` |
| 2 | `step2_context.py` | 히스토리 · 압축 · 회수 | `context growth`, `recall check` |
| 3 | `step3_artifacts.py` | 노트 저장 · 읽기 | `redundant calls`, `artifact reuse` |
| 4 | `step4_planning.py` | 구조화된 계획 · 준수 측정 | `steps to answer`, `plan adherence` |
| 5 | `step5_eval.py` | 골든 세트 · 회귀 검출 | `regressions` |

**순서대로 도세요.** 각 단계의 마지막 줄이 다음 단계가 왜 필요한지 말합니다.

```bash
# 0 — 하네스 없이. 지어낸 인용이 어떻게 생겼는지 본다
python harness/step0_baseline.py --endpoint "$AZURE_AI_PROJECT_ENDPOINT" --questions 2

# 1 — 툴 호출 레이어. 두 번째 명령은 실패 처리만 뺀 것이다
python harness/step1_tools.py --endpoint "$AZURE_AI_PROJECT_ENDPOINT" --questions 2 --show-tools
python harness/step1_tools.py --endpoint "$AZURE_AI_PROJECT_ENDPOINT" --questions 2 --broken-tools

# 2 — 컨텍스트 관리. 세 전략의 context growth 를 비교한다
python harness/step2_context.py --endpoint "$AZURE_AI_PROJECT_ENDPOINT" --strategy all

# 3 — 아티팩트. --no-artifacts 와 마지막 질문의 tool call 수를 비교한다
python harness/step3_artifacts.py --endpoint "$AZURE_AI_PROJECT_ENDPOINT" --questions 3
python harness/step3_artifacts.py --endpoint "$AZURE_AI_PROJECT_ENDPOINT" --questions 3 --no-artifacts

# 4 — 플래닝. 계획 호출 값을 하고도 남는지 본다
python harness/step4_planning.py --endpoint "$AZURE_AI_PROJECT_ENDPOINT" --questions 3
python harness/step4_planning.py --endpoint "$AZURE_AI_PROJECT_ENDPOINT" --questions 3 --no-plan

# 5 — 평가. 기록하고, 무언가 고친 뒤, 무엇이 나빠졌는지 이름으로 듣는다
python harness/step5_eval.py --endpoint "$AZURE_AI_PROJECT_ENDPOINT" --out runs/a.json
#   ... step1_tools.py 의 TOOLS description 을 일부러 망가뜨려 보세요 ...
python harness/step5_eval.py --endpoint "$AZURE_AI_PROJECT_ENDPOINT" --baseline runs/a.json
```

**PowerShell** 은 줄 이음만 `` ` `` 로 바꾸면 동일합니다.

## 파일 구성

```
harness/
  harness_tools.py     grep · sed — 능력. 전 단계 공통이고 절대 바뀌지 않는다
  harness_metrics.py   공용 계측. ToolResult · 지표 계산 · 리포트 형식
  golden.py            질문 6개와 근거. 근거 문단은 실행 시점에 문서에서 잘라낸다
  harness_cli.py       인자와 클라이언트 배선. 하네스가 아니라 보일러플레이트
  harness_loop.py      step 1 이 완성한 루프를 뽑아둔 것. step 2~5 가 가져다 쓴다
  step0_baseline.py … step5_eval.py
```

`harness_tools.py` 와 나머지의 경계가 이 랩의 설계입니다. **능력은 고정, 하네스는 여러분이 짓는 것.**
그래서 어떤 단계가 라운드 수를 줄였다면 검색이 좋아진 게 아니라 — 그럴 수가 없으니 —
하네스가 한 일입니다.

## 지표는 어떻게 계산되나

| 지표 | 계산 |
|---|---|
| `hit` | 정답 문자열이 전부 답변에 있는가. LLM 0콜 |
| `tool error rate` | 디스패처가 거부한 호출 / 전체 툴 호출 |
| `redundant calls` | 앞선 호출과 이름·인자가 완전히 같은 호출 수 |
| `context growth` | 모델 호출 번호에 대한 input tokens 의 최소제곱 기울기 |
| `cached` | `usage.input_tokens_details.cached_tokens` 비율 |
| `artifact reuse` | 노트를 읽은 호출 / 전체 툴 호출 |
| `plan adherence` | 계획한 스텝 중 실제 실행된 비율 |
| `backtracks` | 계획에 없던 호출 수 |
| `regressions` | 이전 실행에서 통과했는데 이번에 실패한 문항 |

**`hit` 부터 `backtracks` 까지 전부 심판 없이 나옵니다.** `step5_eval.py --judge evaluation` 만
`azure-ai-evaluation` 으로 groundedness 계열을 매기고 Foundry Evaluation 탭에 올립니다.
행마다 심판 호출이 붙어 비용이 실제로 드니, 기본값은 `none` 입니다.

```bash
pip install -r harness/requirements.txt   # 채점을 쓸 때만
python harness/step5_eval.py --endpoint "$AZURE_AI_PROJECT_ENDPOINT" --judge evaluation
```

## 알아둘 것

- `--questions` 로 문항 수를 줄이면 그만큼 싸고 빠릅니다. 처음에는 `--questions 2` 를 권합니다.
- 문항이 6개뿐이라 **작은 차이는 운입니다.** 두 설정을 비교할 때 한 문항 차이로 결론 내지 마세요.
- 이 랩은 Foundry 에 agent 도 vector store 도 만들지 않습니다. `--judge evaluation` 이 남기는
  evaluation run 하나가 전부이고, 그건 지우면 안 되는 결과물이라 `--delete` 플래그가 없습니다.
- `.agentignore` 는 파일 이름 패턴이라 `harness/` 를 걸러내지 않습니다. hosted agent 를
  배포한다면 `make stage` 로 ZIP 내용을 확인하세요.
