# Foundry Harness — 틀린 답을 알아보는 하네스를 짓는 랩

모델을 부르는 코드는 다들 한 번쯤 써봤습니다. 그 코드가 **에이전트**가 되려면 주위에
무엇이 더 있어야 하는가 — 이 랩은 그걸 한 층씩 직접 지으면서 답합니다.

## 이 랩이 다루는 실패

에이전트를 처음 만들면 이 순서로 실패합니다.

| | 무엇이 문제인가 | 어떻게 보이는가 |
|---|---|---|
| 1 | 자료가 없다 | 아는 척하며 지어낸다 |
| 2 | 자료는 있는데 **엉뚱한 걸 집어 온다** | 근거까지 달고 자신 있게 틀린다 |
| 3 | 틀린 걸 아무도 안 본다 | 그대로 사용자에게 나간다 |

1번은 툴을 붙이면 끝납니다. 데모가 여기서 멈추는 이유이기도 합니다.
**이 랩의 본론은 2번과 3번입니다.**

보고서는 같은 문단에 전국·수도권·서울 값을 나란히 적고, 차트는 수치와 지역명을 따로
늘어놓습니다. 사람이 급히 읽어도 틀리는 자리이고, 모델은 거기서 자신 있게 틀립니다.
질문 세트(`golden.py`)는 **그런 자리만 골라서** 만들었습니다.

## 진행 순서

| 단계 | 파일 | 짓는 것 | 결과 |
|---|---|---|---|
| 0 | `step0_baseline.py` | (아무것도 없음) | 문서가 없으니 **지어낸다** |
| 1 | `step1_tools.py` | 스키마 · 디스패처 · 루프 · 실패 처리 | 찾아온다. 그런데 **틀린 걸 찾아온다** |
| 2 | `step2_verify.py` | 근거 검증 · 반려 · 재시도 | 하네스가 **오답을 알아채고 다시 찾는다** |
| 3 | `step3_eval.py` | 기록 · 대조 | 바꾼 것이 **나빠졌는지 이름으로 안다** |

**각 단계는 자기 대조군을 들고 있습니다.** 대조군 없이 한 번만 돌리면 그 단계는
아무것도 주장하지 않습니다.

```bash
cd python
export AZURE_AI_PROJECT_ENDPOINT="<foundry-project-endpoint>"

# 0 — 하네스 없이. 지어낸 인용이 어떻게 생겼는지 본다
python harness/step0_baseline.py --endpoint "$AZURE_AI_PROJECT_ENDPOINT" --questions 3

# 1 — 툴을 준다. 답을 찾아오는데, '자신 있게 틀림' 이 몇 개인지 본다
python harness/step1_tools.py --endpoint "$AZURE_AI_PROJECT_ENDPOINT" --questions 3 --show-tools
python harness/step1_tools.py --endpoint "$AZURE_AI_PROJECT_ENDPOINT" --questions 3 --broken-tools

# 2 — 검증을 붙인다. 첫 답과 마지막 답이 어떻게 달라지는지 본다
python harness/step2_verify.py --endpoint "$AZURE_AI_PROJECT_ENDPOINT" --questions 3
python harness/step2_verify.py --endpoint "$AZURE_AI_PROJECT_ENDPOINT" --questions 3 --no-verify

# 3 — 기록하고, 무언가 고친 뒤, 무엇이 나빠졌는지 이름으로 듣는다
python harness/step3_eval.py --endpoint "$AZURE_AI_PROJECT_ENDPOINT" --out runs/a.json
#   ... harness_verify.py 의 판정 지시문을 한 줄 바꿔 보세요 ...
python harness/step3_eval.py --endpoint "$AZURE_AI_PROJECT_ENDPOINT" --baseline runs/a.json
```

**PowerShell** 은 줄 이음만 `` ` `` 로 바꾸면 동일합니다.

## 검증은 정답을 모른다

step 2 의 규칙 하나만 기억하면 됩니다.

> **하네스는 정답을 모릅니다.** 알았다면 모델을 부를 이유가 없습니다.

그래서 검증은 "이 답이 맞는가" 를 묻지 않습니다. 대신 이렇게 묻습니다 —

> **"이 답이 근거로 댄 자리가, 이 답을 실제로 뒷받침하는가?"**

정답 없이 확인할 수 있고, 오답의 상당수가 여기서 걸립니다. 옆 칸 값을 집어 온 답은 대개
근거를 못 대거나, 대더라도 그 자리가 그 값을 그 대상의 것으로 지목해 주지 않기 때문입니다.

검증은 두 겹이고, **싼 것이 먼저 거릅니다.**

| 겹 | 무엇을 보나 | 비용 |
|---|---|---|
| 결정론 검사 | 인용이 있나 · 인용한 줄에 그 수치가 정말 있나 | 모델 호출 0번 |
| 근거 판정 | 그 줄이 질문이 물은 대상의 값이라고 확정해 주나 | 모델 호출 1번 |

순서를 뒤집으면 지어낸 인용 하나를 잡는 데 판정 비용을 냅니다. 그리고 판정자에게는
**정답을 주지 않습니다.** 답안지를 쥔 판정자는 하네스가 아니라 채점 기계이고, 실전에는
그런 게 없습니다.

반려할 때 사유와 다음 할 일을 함께 돌려주는 것도 규칙입니다. "틀렸다" 만 돌려주는 검증은
재시도를 **반복**으로 만들고, 무엇이 왜 부족한지 돌려주는 검증은 재시도를 **조사**로
만듭니다. 이 차이가 step 2 의 전부입니다.

## 검증은 공짜가 아니다

리포트의 **헛수고** 칸을 꼭 보세요. 맞는 답을 반려한 횟수입니다.

|  | 하네스 통과 | 하네스 반려 |
|---|---|---|
| **정답** | 정상 | **헛수고** — 값을 치르고 답을 뭉갠다 |
| **오답** | **위험** — 자신 있게 틀린 답이 나간다 | 잡아냈다 |

step 1 은 왼쪽 아래 칸이 그대로 나가는 상태입니다. step 2 는 그걸 오른쪽 아래로 옮기고,
재시도로 왼쪽 위까지 밀어 올립니다. 그 과정에서 오른쪽 위 칸이 같이 커집니다.
**검증을 조이면 hit 은 그대로면서 헛수고만 오르는 변경이 아주 흔하고**, hit 만 보는
리포트는 그걸 "변화 없음" 이라고 말합니다. step 3 이 그래서 필요합니다.

## 파일 구성

```
harness/
  harness_tools.py     grep · sed — 능력. 전 단계 공통이고 절대 바뀌지 않는다
  harness_verify.py    검증 레이어. 결정론 검사 + 근거 판정
  harness_metrics.py   공용 계측. ToolResult · 지표 계산 · 리포트 형식
  golden.py            함정이 있는 질문 6개와 근거. 근거는 실행 시점에 문서에서 잘라낸다
  harness_cli.py       인자와 클라이언트 배선. 하네스가 아니라 보일러플레이트
  harness_loop.py      step 1 이 완성한 루프를 뽑아둔 것. step 2 · 3 이 가져다 쓴다
  step0_baseline.py … step3_eval.py
```

`harness_tools.py` 와 나머지의 경계가 이 랩의 설계입니다. **능력은 고정, 하네스는 여러분이
짓는 것.** 검색 능력이 한 글자도 안 바뀌므로, 숫자가 움직였다면 그건 하네스가 한 일입니다.
지시문도 step 1 부터 3 까지 같은 문장을 씁니다 — 그래야 hit 이 오른 것을 문구 덕으로
돌릴 수 없습니다.

## 지표는 어떻게 계산되나

| 지표 | 계산 |
|---|---|
| `hit` | 정답 문자열이 전부 답변에 있는가. LLM 0콜 |
| `자신 있게 틀림` | 근거를 달고 낸 오답의 수 (step 1) |
| `첫 답 오답` | 검증이 없었다면 그대로 나갔을 오답의 수 |
| `하네스 반려` | 검증이 되돌려 보낸 횟수. 그중 모델 없이 잡은 것을 따로 센다 |
| `재시도로 교정` | 첫 답이 틀렸다가 마지막에 맞은 질문 수 |
| `헛수고` | **맞는 답**을 반려한 횟수 |
| `tool error rate` | 디스패처가 거부한 호출 / 전체 툴 호출 |
| `regressions` | 이전 실행에서 통과했는데 이번에 실패한 문항 |

**전부 심판 없이 나옵니다.** `step3_eval.py --judge evaluation` 만 `azure-ai-evaluation`
으로 groundedness 계열을 매기고 Foundry Evaluation 탭에 올립니다. 행마다 심판 호출이
붙어 비용이 실제로 드니, 기본값은 `none` 입니다.

```bash
pip install -r harness/requirements.txt   # 채점을 쓸 때만
python harness/step3_eval.py --endpoint "$AZURE_AI_PROJECT_ENDPOINT" --judge evaluation
```

## 알아둘 것

- **`--questions 3` 아래로 줄이지 마세요.** 앞의 세 문항이 함정 문항이고, 그 아래로
  줄이면 이 랩이 보여주려는 실패가 표본에서 빠집니다.
- 문항이 6개뿐이라 **작은 차이는 운입니다.** 두 설정을 비교할 때 한 문항 차이로
  결론 내지 마세요.
- step 2 는 질문마다 최대 3번 시도하고 시도마다 판정 호출이 하나 붙습니다.
  **step 1 보다 몇 배 비쌉니다.** 그게 이 레이어의 청구서이고, 리포트에 그대로 잡힙니다.
- 이 랩은 Foundry 에 agent 도 vector store 도 만들지 않습니다. `--judge evaluation` 이
  남기는 evaluation run 하나가 전부입니다.
