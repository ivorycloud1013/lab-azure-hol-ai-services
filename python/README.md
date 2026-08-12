# Overview

Foundry 을 CLI에서 직접 호출해 보는 hands-on 스크립트 collection 입니다.

# Prerequisite
- Microsoft Foundry workspace ・ project ・ 모델 배포 완료
- 아래 명령어 실행하여 python package dependency resolve
  ```bash
  pip install -r requirements.txt
  az login
  ```

## 공통 인증

이 랩의 Foundry 계정은 키를 꺼 두었기 때문에(`disableLocalAuth=true`) 기본 경로는 Entra ID
토큰입니다. `az login`만 해 두면 아무 인증 인자도 줄 필요가 없습니다.
인증 코드는 [`identity.py`](identity.py) 한 곳에 있고, 아래 인자는 모든 스크립트가 공유합니다.

| 인자 | 기본값 | 설명 |
|---|---|---|
| `--auth` | `default` | `default` · `cli` · `device-code` · `environment` · `managed-identity` · `client-secret` · `client-certificate` · `api-key` · `access-token` |
| `--tenant-id` | `$AZURE_TENANT_ID` | 서비스 주체·디바이스 코드에서 사용 |
| `--client-id` | `$AZURE_CLIENT_ID` | 서비스 주체, 또는 사용자 할당 관리 ID |
| `--client-secret` | `$AZURE_CLIENT_SECRET` | `--auth client-secret` |
| `--certificate-path` | `$AZURE_CLIENT_CERTIFICATE_PATH` | `--auth client-certificate` |
| `--api-key` | `$AZURE_OPENAI_API_KEY` | 키를 끈 계정에서는 401 |
| `--access-token` | `$AZURE_OPENAI_ACCESS_TOKEN` | 이미 받아 둔 토큰 |

## Foundry Models
Foundry 의 **모델 호출(`hol-foundry-models-*.py`)** 을 다룹니다.

| File name | What to do |
|---|---|
| [`hol-foundry-models-llm.py`](#hol-foundry-models-llmpy) | LLM Q&A Pipeline |
| [`hol-foundry-models-optimize-reasoning.py`](#hol-foundry-models-optimize-reasoningpy) | LLM 추론·출력량 조절 옵션 비교 |
| [`hol-foundry-models-optimize-token.py`](#hol-foundry-models-optimize-tokenpy) | LLM 프롬프트 캐시와 구조화 출력으로 토큰 줄이기 |
| [`hol-foundry-models-vlm.py`](#hol-foundry-models-vlmpy) | Image 생성·편집 |
| [`hol-foundry-models-stt_tts.py`](#hol-foundry-models-stt_ttspy) | 음성 합성(TTS), 음성 인식(STT) |

### hol-foundry-models-llm.py

시스템 · 사용자 프롬프트를 보내고 응답을 받습니다.

```mermaid
%%{init: {"theme": "neutral"}}%%
flowchart LR
    USER["cmdline arguments"] --> CLIENT["OpenAI Client"] --> CALL["chat.completions.create()"] --> OUT["Response"]
```

| 인자 | 기본값 | 설명 |
|---|---|---|
| `--endpoint` | (필수) | Foundry Azure OpenAI 엔드포인트 |
| `--deployment` | `gpt-5.4` | 모델 Deployment 이름 |
| `--system` | `You are a helpful assistant.` | System 프롬프트 |
| `--user` | (필수) | User 프롬프트 |
| `--temperature` | 모델 default | |
| `--max-tokens` | 모델 default | |
| `--stream` | false | streaming 방식으로 token 수신 |

```bash
python hol-foundry-models-llm.py \
  --endpoint "<foundry-aoai-endpoint>" \
  --deployment "<model-deployment-name>" \
  --user "Azure Private Endpoint 를 세 문장으로 설명해줘."
```

---

### hol-foundry-models-vlm.py

텍스트 프롬프트와 이미지를 보내고 응답을 받습니다.

```mermaid
%%{init: {"theme": "neutral"}}%%
flowchart LR
    USER["cmdline arguments"] --> CLIENT["OpenAI Client"] --> CALL["images.generate() </br> or images.edit()"] --> OUT["Response"]
```

| 인자 | 기본값 | 설명 |
|---|---|---|
| `--endpoint` | (필수) | Foundry Azure OpenAI 엔드포인트 |
| `--deployment` | `gpt-image-2` | 모델 Deployment 이름 |
| `--method` | `generate` | `generate` 또는 `edit` |
| `--prompt` / `--prompt-file` | (둘 중 하나 필수) | 프롬프트를 직접 주거나 텍스트 파일에서 읽기 |
| `--image` | — | `--method edit`의 원본 이미지 |
| `--mask` | — | 교체할 영역을 표시한 PNG |
| `--out` | `image.png` | 출력 파일 |
| `--size` | `1024x1024` | |
| `--quality` | `low` | `low` · `medium` · `high` |
| `--count` | `1` | 받을 이미지 개수 |

```bash
# 생성 : 기본
python hol-foundry-models-vlm.py \
  --endpoint "<foundry-aoai-endpoint>" \
  --deployment "<model-deployment-name>" \
  --prompt "겨울 산 위의 데이터센터, 수채화" \
  --out datacenter.png

# 생성 : prompt 파일
python hol-foundry-models-vlm.py \
  --endpoint "<foundry-aoai-endpoint>" \
  --deployment "<model-deployment-name>" \
  --prompt-file assets/models/creative-01.txt \
  --quality high --count 2

# 편집
python hol-foundry-models-vlm.py \
  --endpoint "<foundry-aoai-endpoint>" \
  --deployment "<model-deployment-name>" \
  --method edit \
  --prompt-file assets/models/style-prompt.txt \
  --image assets/models/style-001.jpg
```

---

### hol-foundry-models-stt_tts.py

Azure Speech로 텍스트를 소리로(TTS), 소리를 텍스트로(STT) 바꿉니다.
`--tts-input`과 `--stt-input` 중 **정확히 하나**만 줍니다.

```mermaid
%%{init: {"theme": "neutral"}}%%
flowchart LR
    subgraph TTS
        direction LR
        USER1["cmdline arguments"] --> SYNTH["SpeechSynthesizer"] --> TOUT["Output audio"]
    end
    subgraph STT
        direction LR
        USER2["cmdline arguments"] --> REC["SpeechRecognizer"] --> PARTS["Output text"]
    end
```

| 인자 | 기본값 | 설명 |
|---|---|---|
| `--endpoint` | (필수) | Foundry Speech 엔드포인트 |
| `--tts-input` | — | Text-to-Speech 입력 텍스트 |
| `--tts-output` | `speech.wav` | Text-to-Speech 출력 파일 |
| `--tts-voice` | `en-US-Ava:DragonHDLatestNeural` | voice 타입 |
| `--stt-input` | — | Speech-to-Text 입력 파일 |
| `--stt-lang` | `ko-KR` | Speech-to-Text 입력 언어 |
| `--stt-phrase` | — | 입력 단어 phrase list |
| `--stt-silence-ms` | SDK default | input 무음 구간 |
| `--stt-detailed` | 끔 | confidence 를 비롯한 세부사항을 출력 |
| `--stt-any-format` | 끔 | 16 kHz 모노가 아닌 파일도 그대로 전송 |
| `--no-post-refine` | 끔 | TrueText 후처리 끔 |

```bash
# STT — 한국어 음성으로
python hol-foundry-models-stt_tts.py \
  --endpoint "<foundry-aoai-endpoint>" \
  --stt-input "assets/models/갤럭시Z 폴드8·플립8, 내일부터 사전 판매@2026.07.27.wav"

# TTS — STT 로 출력된 텍스트 되받아 적기
python hol-foundry-models-stt_tts.py \
  --endpoint "<foundry-aoai-endpoint>" \
  --tts-input "..."
```

---

### hol-foundry-models-optimize-reasoning.py

Responses API의 조절 옵션을 하나씩 실행하고, 그때마다 `usage`를 찍어 줍니다.
무엇이 얼마나 토큰을 쓰는지 눈으로 보는 것이 목적입니다.

```mermaid
%%{init: {"theme": "neutral"}}%%
flowchart LR
    DEMO["Execute"] --> V["verbosity 에 따른 token usage 비교"]
    DEMO --> M["max-tokens 설정에 따른 output 변화"]
    DEMO --> C["output grammar 로 출력 형태를 formatting"]
    DEMO --> R["reasoning effort 에 따른 output 비교"]
    DEMO --> P["parallel tool calling 동작 확인"]
```

| 인자 | 기본값 | 설명 |
|---|---|---|
| `--endpoint` | (필수) | Foundry Azure OpenAI 엔드포인트 |
| `--deployment` | `gpt-5.4` | 모델 Deployment 이름 |
| `--demo` | `all` | `verbosity` · `max-tokens` · `cfg` · `reasoning` · `parallel-tools` · `all` |

```bash
# 다섯 개 전부
python hol-foundry-models-optimize-reasoning.py \
  --endpoint "<foundry-aoai-endpoint>" \
  --deployment "<model-deployment-name>"
```

---

### hol-foundry-models-optimize-token.py

프롬프트 캐시가 실제로 걸리는지, 그리고 구조화 출력이 모델마다 몇 토큰을 쓰는지 재 봅니다.

```mermaid
%%{init: {"theme": "neutral"}}%%
flowchart LR
    DEMO["Execute"] --> A["input token implicit caching"]
    DEMO["Execute"] --> B["input token explicit caching"]
    DEMO["Execute"] --> S["structured output"]
```

| 인자 | 기본값 | 설명 |
|---|---|---|
| `--endpoint` | (필수) | Foundry Azure OpenAI 엔드포인트 |
| `--deployment` | `gpt-5.4` | 모델 Deployment 이름 |
| `--demo` | `all` | `caching` · `cache-retention` · `cache-key` · `structured` · `all` |
| `--rounds` | `10` | input token caching 반복 횟수 |
| `--cache-key` | `prompt-cache-key-1` | 요청을 한 엔드포인트에 붙여 두는 키 |
| `--structured-deployments` | `gpt-4.1 gpt-5.4` | 같은 구조화 출력으로 비교할 모델 Deployment 들 |

```bash
# 전부
python hol-foundry-models-optimize-token.py \
  --endpoint "<foundry-aoai-endpoint>" \
  --deployment "<model-deployment-name>"
```

## Foundry Agents
Foundry 의 **에이전트(`hol-foundry-agents-*.py`)** 를 다룹니다.
세 스크립트 모두 **markdown 문서 하나를 근거로 답하는 같은 RAG 에이전트**입니다.

| File name | What to do |
|---|---|
| [`hol-foundry-agents-prompt.py`](#hol-foundry-agents-promptpy) | Foundry 에 prompt agent (declarative) 를 만들고 File Search 로 답하기 |
| [`hol-foundry-agents-responses.py`](#hol-foundry-agents-responsespy) | Azure OpenAI Responses API 를 이용한 로컬 Agent 만들기 |
| [`hol-foundry-agents-hosted.py`](#hol-foundry-agents-hostedpy) | Agent Framework 로 만든 hosted agent 로컬 버전 |

### hol-foundry-agents-prompt.py

문서를 업로드해 vector store 로 색인하고, 그 위에 prompt agent 를 선언합니다.
검색도 응답도 전부 서비스에서 일어나므로 이 프로세스는 질문만 던집니다.

```mermaid
%%{init: {"theme": "neutral"}}%%
flowchart TD
    USER["cmdline arguments"] --> UP["OpenAI.files.create()"] --> AG["AIProjectClient.agents.create_version()"] --> CONV["AIProjectClient.get_openai_client().conversations.create()"] --> ASK["AIProjectClient.get_openai_client().responses.create()"] --> OUT["Response"]
```

| 인자 | 기본값 | 설명 |
|---|---|---|
| `--endpoint` | (필수) | Foundry project 엔드포인트 |
| `--deployment` | `gpt-5.6-terra` | 모델 Deployment 이름 |
| `--file` | — | 업로드할 파일 |
| `--agent-name` | `hol-md-rag` | Foundry agent 이름 |
| `--question` | (필수) | 질문 |
| `--delete` | 끔 | 끝나고 agent · vector store · file 까지 정리 |

- vector store 색인에는 계정에 **embedding deployment** (`text-embedding-3-large`) 가 있어야 합니다.

```bash
# 최초 실행 : 문서를 올리며 agent 생성
python hol-foundry-agents-prompt.py \
  --endpoint "<foundry-project-endpoint>" \
  --file "assets/tools/KB주택시장리뷰_2025년 10월호.md" \
  --question "2025년 10월 서울 아파트 매매가격 흐름을 요약해줘."

# 재사용 : --file 없이, 이어서 두 번 묻기
python hol-foundry-agents-prompt.py \
  --endpoint "<foundry-project-endpoint>" \
  --question "전세 시장은 어땠어?" \
  --question "그 근거가 된 문장을 그대로 인용해줘."

# 정리 : agent · vector store · file 삭제
python hol-foundry-agents-prompt.py \
  --endpoint "<foundry-project-endpoint>" \
  --question "마지막 질문" \
  --delete
```

---

### hol-foundry-agents-responses.py

로컬 Agent 를 구축하여 OpenAI Responses API 를 사용합니다. Tool calling 시에 RAG pipeline 을 위해 `grep` · `sed` 로 로컬 파일을 읽어 결과를 돌려 줍니다.

```mermaid
%%{init: {"theme": "neutral"}}%%
flowchart LR
    USER["cmdline arguments"] --> REQ["OpenAI.responses.create()"] --> OUT["Response"]
```

| 인자 | 기본값 | 설명 |
|---|---|---|
| `--endpoint` | (필수) | Foundry project 엔드포인트 |
| `--deployment` | `gpt-5.6-terra` | 모델 Deployment 이름 |
| `--file` | (필수) | 검색할 로컬 markdown |
| `--question` | (필수) | 질문 |
| `--show-tools` | 끔 | 모델이 돌린 검색을 그대로 출력 |

- tool loop 는 최대 8 라운드입니다. 넘으면 질문을 좁히라는 메시지와 함께 종료합니다.
- `subprocess` 를 shell 없이 실행하므로 모델이 만든 패턴이 다른 명령으로 번지지 않습니다.

```bash
# 기본
python hol-foundry-agents-responses.py \
  --endpoint "<foundry-aoai-endpoint>" \
  --file "assets/tools/KB주택시장리뷰_2025년 10월호.md" \
  --question "월세 지수는 어떻게 움직였어?"
```

---

### hol-foundry-agents-hosted.py

앞의 responses 예제와 같은 도구를 Agent Framework 의 `Agent` 로 감싼 것입니다.
파일 하나가 두 가지로 동작합니다 — `--question` 을 주면 한 번 답하고 끝나고,
주지 않으면 responses 프로토콜을 서빙합니다. Foundry 는 이 서빙 경로로 에이전트를 호출합니다.

```mermaid
%%{init: {"theme": "neutral"}}%%
flowchart LR
    SRC["create agent_framework.Agent"] --> START["ResponsesHostServer(agent).run()"] --> OUTPUT["Request&Response"]
```

| 인자 | 기본값 | 설명 |
|---|---|---|
| `--endpoint` | `$FOUNDRY_PROJECT_ENDPOINT` · `$AZURE_AI_PROJECT_ENDPOINT` | Foundry project 엔드포인트 |
| `--deployment` | `$FOUNDRY_MODEL_NAME` · `$AZURE_AI_MODEL_DEPLOYMENT_NAME` · `gpt-5.6-terra` | 모델 Deployment 이름 |
| `--file` | `$AGENT_DOCUMENT` | 검색할 markdown |
| `--question` | — | 주면 한 번 답하고 종료, 없으면 서버로 동작 |
| `--port` | `$PORT` · `8088` | 서빙 포트 |
| `--host` | `0.0.0.0` | bind 주소 (호스팅될 때 `0.0.0.0`) |

배포에 필요한 것들은 [`azure.yaml`](azure.yaml) · [`.agentignore`](.agentignore) · [`Makefile`](Makefile) 에 있습니다.
Foundry project 와 모델 배포는 `iac/` 가 만들고, azd 는 **에이전트만** 빌드·호스팅합니다.
`.agentignore` 덕분에 ZIP 에는 `hol-foundry-agents-hosted.py` · `identity.py` · `requirements.txt` ·
`assets/document.md` 네 개만 올라갑니다.

```bash
# 사전 준비
az login && azd auth login
azd ext install microsoft.foundry
export AZURE_AI_PROJECT_ENDPOINT="<foundry-project-endpoint>"
export AZURE_AI_MODEL_DEPLOYMENT_NAME="<model-deployment-name>"
```

| Makefile target | 하는 일 |
|---|---|
| `make ask-local FILE=… QUESTION=…` | 컨테이너와 같은 코드 경로로 한 번만 답하기 (서버 없음) |
| `make local FILE=…` | `localhost:8088` 에 서빙, 배포는 하지 않음 |
| `make ask FILE=… QUESTION=…` | 같은 문서를 responses 예제로 물어보기 |
| `make stage FILE=…` | `assets/document.md` 로 staging 하고 실제 업로드 목록 확인 |
| `make provision` | 기존 project 위에 azd 환경 생성 |
| `make deploy FILE=…` | 패키징해서 Foundry Agent Service 에 배포 |
| `make invoke QUESTION=…` | 배포된 에이전트에 질문 |
| `make monitor` | 호스팅된 컨테이너 로그 따라가기 |
| `make down` | azd 가 만든 것 제거 |

```bash
# 스크립트를 직접 서빙 (Makefile 없이)
python hol-foundry-agents-hosted.py \
  --endpoint "<foundry-project-endpoint>" \
  --file "assets/tools/KB주택시장리뷰_2025년 10월호.md" \
  --port 8088

# 배포하고 물어보기
make deploy FILE="assets/tools/KB주택시장리뷰_2025년 10월호.md"
make invoke QUESTION="2025년 10월 전세 시장을 요약해줘."
```

## Foundry IQ & Tools
Foundry 의 IQ 와 Tools 를 다룹니다. 문서를 데이터로 바꾸고(Content Understanding), 그 데이터를 지식으로 붙이고(Knowledge),
외부 시스템을 도구로 붙이고(MCP), 그 결과를 목소리로 주고받는(Voice) 순서입니다.

| File name | What to do |
|---|---|
| [`hol-foundry-tools-content-understanding.py`](#hol-foundry-tools-content-understandingpy) | 문서에서 본문 · 필드 추출 |
| [`hol-foundry-tools-knowledge.py`](#hol-foundry-tools-knowledgepy) | Azure AI Search 인덱스 · Bing 을 agent 지식으로 붙이기 |
| [`hol-foundry-tools-mcp.py`](#hol-foundry-tools-mcppy) | 원격 MCP 서버를 agent 도구로 붙이기 |
| [`hol-foundry-tools-voice.py`](#hol-foundry-tools-voicepy) | Voice Live API 로 모델 · agent 와 음성 대화 |

### hol-foundry-tools-content-understanding.py

문서를 그대로 올려 markdown 과 구조화 필드를 받아옵니다. 입력 문서는 `.md` · `.json` 두 개로 enrichment 됩니다.

```mermaid
%%{init: {"theme": "neutral"}}%%
flowchart TB
    USER["cmdline arguments"] --> PRE["ContentUnderstandingClient() 생성"] --> RUN["ContentUnderstandingClient.begin_analyze()"] --> OUT["Output .md + .json"]
```

| 인자 | 기본값 | 설명 |
|---|---|---|
| `--endpoint` | (필수) | Foundry project 엔드포인트 |
| `--file` | (필수) | 분석할 문서, 여러 개면 반복 지정 |
| `--analyzer` | `prebuilt-document` | 이미 있는 analyzer id |
| `--out-dir` | 원본 문서 옆 | `.md` · `.json` 출력 디렉터리 |
| `--api-version` | `2025-11-01` | |

```bash
# 기본 : prebuilt-document 로 markdown 뽑기
python hol-foundry-tools-content-understanding.py \
  --endpoint "<foundry-project-endpoint>" \
  --file "assets/agents/2026 휴식이 있는 캘린더.pdf" \
  --out-dir assets/tools

# 다른 prebuilt analyzer 로 : 레이아웃만 (모델 배포 불필요)
python hol-foundry-tools-content-understanding.py \
  --endpoint "<foundry-project-endpoint>" \
  --analyzer prebuilt-layout \
  --file "assets/agents/하도급거래 공정화에 관한 법률(법률)(제21060호)(20251217).pdf" \
  --out-dir assets/tools
```

---

### hol-foundry-tools-knowledge.py

Azure AI Search 인덱스(그리고 선택적으로 Bing)를 prompt agent 의 knowledge 으로 연결합니다.

```mermaid
%%{init: {"theme": "neutral"}}%%
flowchart TB
    USER["cmdline arguments"] --> AG["AIProjectClient.agents.create_version()"] --> CONV["AIProjectClient.get_openai_client().conversations.create()"] --> ASK["AIProjectClient.get_openai_client().responses.create()"] --> OUT["Response + citations"]
```

| 인자 | 기본값 | 설명 |
|---|---|---|
| `--endpoint` | (필수) | Foundry project 엔드포인트 |
| `--deployment` | `gpt-5.6-terra` | 모델 Deployment 이름 |
| `--index` | — | 근거로 삼을 Azure AI Search 인덱스 (`housing` · `merchants` · `news`) |
| `--search-connection` | project 의 기본 Search 연결 | Search 서비스로의 project connection 이름 |
| `--bing-connection` | — | Grounding with Bing Search 연결 이름, 공개 웹까지 함께 검색 |
| `--agent-name` | `hol-knowledge-rag` | 버전을 만들 agent 이름 |
| `--question` | (필수) | 질문, 반복하면 같은 대화에서 이어 묻기 |
| `--show-sources` | 끔 | agent 가 돌린 검색과 인용을 그대로 출력 |
| `--delete` | 끔 | 끝나고 agent 삭제 |

- 인덱스는 [`aisrch-init-upload-documents.py`](aisrch-init-upload-documents.py) 가 먼저 만들어 둔 것을 씁니다.
- 검색은 **project identity** 로 수행되므로, 내가 아니라 project 에 Search 서비스의 `Search Index Data Reader` 가 필요합니다.

```bash
# 인덱스 하나로 묻기
python hol-foundry-tools-knowledge.py \
  --endpoint "<foundry-project-endpoint>" \
  --index housing \
  --question "2025년 10월 서울 아파트 매매가격 흐름을 요약해줘."

# 인덱스 + 공개 웹, 그리고 정리
python hol-foundry-tools-knowledge.py \
  --endpoint "<foundry-project-endpoint>" \
  --index news \
  --bing-connection "<bing-connection-name>" \
  --question "최근 기술 뉴스 흐름을 정리해줘."
```

---

### hol-foundry-tools-mcp.py

원격 MCP 서버를 prompt agent 의 tool 로 선언합니다.

```mermaid
%%{init: {"theme": "neutral"}}%%
flowchart TB
    USER["cmdline arguments"] --> AG["AIProjectClient.agents.create_version()"] --> ASK["AIProjectClient.get_openai_client().responses.create()"] --> OUT["Response"]
```

| 인자 | 기본값 | 설명 |
|---|---|---|
| `--endpoint` | (필수) | Foundry project 엔드포인트 |
| `--deployment` | `gpt-5.6-terra` | 모델 Deployment 이름 |
| `--mcp` | — | `LABEL=URL` 또는 `LABEL=URL=AUDIENCE`, 반복 가능 |
| `--connection` | — | `LABEL=CONNECTION_ID`, project 가 이미 가진 연결, 반복 가능 |
| `--allowed-tool` | — | 모든 서버를 이 이름의 도구로 제한, 반복 가능 |
| `--read-only` | 끔 | 서버가 read-only 로 표시한 도구만 허용 |
| `--agent-name` | `hol-mcp-ops` | 버전을 만들 agent 이름 |
| `--question` | (필수) | 질문, 반복하면 같은 대화에서 이어 묻기 |
| `--show-tools` | 끔 | 발견한 도구 목록과 호출 내역 출력 |
| `--delete` | 끔 | 끝나고 agent 삭제 |

- `--mcp` · `--connection` 중 **최소 하나**는 있어야 하고, 섞어 써도 됩니다.
- 인증 방식이 셋의 차이입니다 — `AUDIENCE` 없는 `--mcp` 는 공개·무인증(예: Microsoft Learn), `AUDIENCE` 를 준 `--mcp` 는 **내 Entra 토큰**을 정의에 심어 두므로 토큰이 만료되면 그 버전은 동작을 멈추고, `--connection` 은 project identity 로 인증해 계속 동작합니다.
- 그래서 실행할 때마다 새 버전을 만듭니다.
- `--allowed-tool` 과 `--read-only` 는 함께 쓸 수 없습니다.
- 도구 호출 승인은 `never` 입니다 — 터미널에서 자문자답하는 랩에는 승인해 줄 사람이 없으니, 읽기 전용 서버와 함께 쓰세요.
- `--auth api-key` · `--auth access-token` 은 projects SDK 가 지원하지 않습니다.

```bash
# 가장 간단 : 공개 Microsoft Learn MCP 서버 (인증 불필요)
python hol-foundry-tools-mcp.py \
  --endpoint "<foundry-project-endpoint>" \
  --mcp "learn=https://learn.microsoft.com/api/mcp" \
  --question "Azure Private Endpoint 와 Service Endpoint 차이를 문서 기준으로 알려줘."

# 도구 호출 과정까지 보기
python hol-foundry-tools-mcp.py \
  --endpoint "<foundry-project-endpoint>" \
  --mcp "learn=https://learn.microsoft.com/api/mcp" \
  --show-tools \
  --question "Foundry Agent Service 의 지원 리전을 알려줘."

# 여러 서버 함께 붙이기 + 읽기 전용 제한
python hol-foundry-tools-mcp.py \
  --endpoint "<foundry-project-endpoint>" \
  --mcp "learn=https://learn.microsoft.com/api/mcp" \
  --mcp "myapi=https://<my-mcp-host>/mcp=https://<my-api-audience>" \
  --connection "internal=<project-connection-id>" \
  --read-only \
  --question "두 소스를 비교해서 정리해줘." \
  --delete
```

---

### hol-foundry-tools-voice.py

Voice Live API 로 **웹소켓 하나에 음성 입력과 음성 출력을 함께** 실어 대화합니다.
STT·TTS 를 따로 호출하는 [`hol-foundry-models-stt_tts.py`](#hol-foundry-models-stt_ttspy) 와 대비되는 경로입니다.

대답하는 쪽은 셋 중 하나입니다.

| 쓰는 인자 | 대답하는 쪽 |
|---|---|
| **`--project-endpoint`** | knowledge · mcp 예제처럼 **이 스크립트가 `create_version()` 으로 만든 음성 전용 agent** |
| `--agent-name` + `--project-name` | 이미 있는 agent (예: `hol-knowledge-rag`) |
| (없음) · `--model` | realtime 모델 직결 |

첫 줄이 Agent Service 경로입니다 — project 에 prompt agent 를 만들고, 방금 만든 그 버전에 붙어 대화합니다.
음성 처리 자체는 어느 쪽이든 Voice Live 가 맡으므로 `--endpoint` 는 project 가 아니라 **계정** 엔드포인트이고,
agent 를 만들 project 는 `--project-endpoint` 로 따로 받습니다. project 이름은 그 URL 끝에서 읽어냅니다.

```mermaid
%%{init: {"theme": "neutral"}}%%
flowchart TB
    NEW["--project-endpoint"] --> CREATE["AIProjectClient.agents.create_version()"] --> WS
    OLD["--agent-name + --project-name"] --> WS
    BARE["--model"] --> WS
    MIC["Microphone"] --> WS["voicelive.connect() </br> single websocket"]
    WS --> TEXT["Transcript"]
    WS --> SPK["Speaker"]
```

| 인자 | 기본값 | 설명 |
|---|---|---|
| `--endpoint` | (필수) | Foundry **계정** 엔드포인트 (`https://<resource>.cognitiveservices.azure.com`) |
| `--model` | `gpt-realtime` | 대화할 realtime 모델 |
| `--project-endpoint` | — | 여기에 agent 를 만들어 대화 (`https://<resource>.services.ai.azure.com/api/projects/<project>`) |
| `--deployment` | `gpt-5.6-terra` | 만들어질 agent 가 쓸 모델 Deployment |
| `--agent-name` | 만들 때 `hol-voice-agent` | 만들 agent 이름, 또는 이미 있는 agent 이름 |
| `--project-name` | — | `--agent-name` 이 있는 project (`--project-endpoint` 없을 때) |
| `--delete` | 끔 | `--project-endpoint` 로 만든 agent 를 끝나고 삭제 |
| `--seconds` | — | N 초 뒤 종료 (없으면 Ctrl+C 까지) |
| `--voice` | `en-US-AvaMultilingualNeural` | Azure voice |
| `--language` | 자동 감지 | 입력 음성 언어, 예 `ko-KR` |
| `--instructions` | 랩 기본 프롬프트 | 어시스턴트 역할 재정의 |

- 입력은 **마이크**, 출력은 **스피커** 입니다. `sounddevice` 패키지와 사운드카드가 필요하므로, Bastion 으로 접속한 점프박스에서는 동작하지 않습니다.
- 서비스가 VAD 로 턴을 끊고 barge-in(말 끊기)이 동작합니다 — 어시스턴트가 말하는 중에 말을 걸면 재생이 멈춥니다.
- `--project-endpoint` 는 매 실행마다 새 버전을 만들고, 방금 만든 그 버전에 붙습니다. `--instructions` 는 세션이 아니라 **agent 정의**에 들어갑니다.
- `--agent-name` 과 `--project-name` 은 한 쌍이고, agent 가 이미 모델을 갖고 있으므로 `--model` 과는 함께 쓸 수 없습니다.
- 이미 있는 agent 에 붙을 때는 그 agent 의 instructions 를 덮어쓰지 않습니다 — `--instructions` 를 명시했을 때만 바뀝니다.
- agent 경로는 **Entra ID 전용**입니다. 키 인증을 지원하지 않으므로 `--auth api-key` 를 주면 실행 전에 막힙니다. 계정에 `Foundry User` 역할이 필요합니다.
- 실행 도중 실패하면 이번 실행이 **새로 만든** agent 만 지웁니다. 이미 있던 agent 에 버전만 더한 경우는 남겨 둡니다.
- `--auth access-token` 은 Voice Live SDK 가 지원하지 않습니다.

```bash
# Agent Service : 음성 전용 agent 를 만들어 대화
python hol-foundry-tools-voice.py \
  --endpoint "<foundry-account-endpoint>" \
  --project-endpoint "<foundry-project-endpoint>" \
  --language ko-KR

# Agent Service : 이름·모델·역할을 정해 만들고, 30초 뒤 정리까지
python hol-foundry-tools-voice.py \
  --endpoint "<foundry-account-endpoint>" \
  --project-endpoint "<foundry-project-endpoint>" \
  --agent-name hol-voice-demo \
  --deployment "<model-deployment-name>" \
  --instructions "너는 Azure 상담원이다. 두 문장 안에 답한다." \
  --seconds 30 \
  --delete

# 이미 있는 agent : knowledge 예제가 남긴 agent 에게 말로 묻기
python hol-foundry-tools-voice.py \
  --endpoint "<foundry-account-endpoint>" \
  --agent-name hol-knowledge-rag \
  --project-name "<project-name>"

# 모델 직결 : agent 없이 realtime 모델과 대화
python hol-foundry-tools-voice.py \
  --endpoint "<foundry-account-endpoint>" \
  --seconds 30
```

## Foundry Observability
Foundry 의 client-side tracing 을 다룹니다.

| File name | What to do |
|---|---|
| [`hol-foundry-observability-single.py`](#hol-foundry-observability-singlepy) | Microsoft Foundry 내 single agent tracing 하기 |
| [`hol-foundry-observability-multi.py`](#hol-foundry-observability-multipy) | Microsoft Foundry 내 multi-agent 개별 tracing 하기 |
| [`hol-foundry-observability-propagation.py`](#hol-foundry-observability-propagationpy) | Microsoft Foundry 내 multi-agent 통합 tracing 하기 |
| [`hol-foundry-observability-af-single.py`](#hol-foundry-observability-af-singlepy) | Azure Monitor 로 Microsoft Agent Framework agent tracing 하기 |

### hol-foundry-observability-single.py

agent 하나가 tool 하나를 부르는 가장 짧은 실행입니다. tool loop 를 이 파일이 직접 돌리므로,
한 번의 질문이 `responses.create()` 2번이 되고 span 도 그만큼 남습니다.

```mermaid
%%{init: {"theme": "neutral"}}%%
flowchart TB
    SETUP["Exporter 설정"] --> INST["AIProjectInstrumentor().instrument()"] --> ROOT["span('Scenario: single agent') 설정"] --> C["AIProjectClient.agents.create_version()"] --> I1["AIProjectClient.get_openai_client().responses.create()"] --> T["execute_tool fetch_weather()"] --> I2["AIProjectClient.get_openai_client().responses.create()"]
```

| 인자 | 기본값 | 설명 |
|---|---|---|
| `--endpoint` | (필수) | Foundry project 엔드포인트 |
| `--deployment` | `gpt-5.6-terra` | 모델 Deployment 이름 |
| `--export` | `azure-monitor` | `azure-monitor` · `console` |
| `--question` | `What is the weather in Seoul, and should I take an umbrella?` | 질문 |
| `--delete` | 끔 | 끝나고 agent · conversation 정리 |

- 이 스크립트는 [`identity.py`](identity.py) 를 쓰지 않습니다. `DefaultAzureCredential` 고정이라
  위의 **공통 인증** 인자(`--auth` 등)가 없고, `az login` 만 해 두면 됩니다.
- `--export azure-monitor` 는 project 에 연결된 Application Insights 로 보내고, 그때서야 **Foundry 포털 > Traces** 에 뜹니다 — 2~5분 걸립니다.
  연결 문자열은 project 에게 물어보므로(`project.telemetry.get_application_insights_connection_string()`) 따로 복사해 둘 것이 없고,
  Application Insights 가 연결되지 않은 project 에서는 이 지점에서 실패합니다.
  `--export console` 은 span 을 터미널에 그대로 찍고 비용이 들지 않습니다 — 포털까지 가지 않고 모양만 볼 때 씁니다.
- 프롬프트 · 응답 본문은 기록하지 않습니다(`enable_content_recording=False`). 켜는 인자를 두지 않은 것은
  이 랩이 공용 Application Insights 로 내보내기 때문입니다.
- **exporter 를 먼저 세우고 `instrument()` 를 나중에** 호출합니다.
  OpenTelemetry 는 no-op provider 로 시작하고, 거기 넘긴 span 은 **오류 없이 사라지기** 때문입니다.
- 같은 이유로 `settings.tracing_implementation = "opentelemetry"` 와 `AZURE_EXPERIMENTAL_ENABLE_GENAI_TRACING=true` 가
  azure-core 클라이언트보다 먼저 와야 합니다. 빠뜨려도 실행은 멀쩡해 보이고, SDK 가 그리는 `create_agent` · `invoke_agent` span 만 없습니다.
- tool span 은 `@trace_function` 이 아니라 손으로 엽니다. 데코레이터는 이 호출을 **코드**로 기록해서
  포털 Traces 가 분류할 카테고리를 찾지 못하고 "other" 로 보여 줍니다.
  `gen_ai.operation.name = execute_tool` 을 직접 넣어야 옆의 모델 호출들과 나란히 tool 호출로 뜹니다.
- tool 은 이 프로세스에서 돌기 때문에, 걸린 시간은 서비스 쪽 어디에도 남지 않습니다.
- tool 은 **agent 정의**에 붙습니다. agent reference 와 tools 목록을 함께 실은 `responses.create()` 는 서비스가 거부합니다.
- 기본은 **남기는** 쪽입니다 — span 은 어느 쪽이든 exporter 에 도달하지만, 포털은 아직 존재하는 것만 목록에 올립니다.
  남겨 두면 다음 실행이 그 위에 새 agent 버전을 쌓으므로, 실습을 끝낼 때 `--delete` 로 정리합니다.

```bash
# 기본 : Application Insights 로 보내고 agent 는 남기기
python hol-foundry-observability-single.py \
  --endpoint "<foundry-project-endpoint>"

# span 모양만 터미널에서 보기
python hol-foundry-observability-single.py \
  --endpoint "<foundry-project-endpoint>" \
  --export console

# 정리 : agent · conversation 삭제
python hol-foundry-observability-single.py \
  --endpoint "<foundry-project-endpoint>" \
  --delete
```

---

### hol-foundry-observability-multi.py

같은 설정에 agent 를 셋으로 늘린 것입니다. 서비스 입장에서는 **서로 무관한 호출 세 번**이고,
이것이 하나의 pipeline 이라는 사실은 이 파일만 압니다. `agent_to_agent_interaction` span 이 그 사실을 trace 에 적어 넣는 자리입니다.

```mermaid
%%{init: {"theme": "neutral"}}%%
flowchart TB
    ROOT["Scenario: multi agent (root span)"] --> C["create_agent × 3"]
    ROOT --> R["invoke_agent researcher"]
    ROOT --> H1["agent_to_agent_interaction"]
    ROOT --> A["invoke_agent analyst"]
    ROOT --> H2["agent_to_agent_interaction"]
    ROOT --> W["invoke_agent writer"]
```

| 인자 | 기본값 | 설명 |
|---|---|---|
| `--endpoint` | (필수) | Foundry project 엔드포인트 |
| `--deployment` | `gpt-5.6-terra` | 모델 Deployment 이름 |
| `--export` | `azure-monitor` | `azure-monitor` · `console` |
| `--task` | checkout p95 latency 회귀 시나리오 | 세 agent 에게 줄 과제 |
| `--delete` | 끔 | 끝나고 agent 셋 · conversation 정리 |

- 이 스크립트는 [`identity.py`](identity.py) 를 쓰지 않습니다. `DefaultAzureCredential` 고정이라
  위의 **공통 인증** 인자(`--auth` 등)가 없고, `az login` 만 해 두면 됩니다.
- `--export azure-monitor` 는 project 에 연결된 Application Insights 로 보내고, 그때서야 **Foundry 포털 > Traces** 에 뜹니다 — 2~5분 걸립니다.
  연결 문자열은 project 에게 물어보므로(`project.telemetry.get_application_insights_connection_string()`) 따로 복사해 둘 것이 없고,
  Application Insights 가 연결되지 않은 project 에서는 이 지점에서 실패합니다.
  `--export console` 은 span 을 터미널에 그대로 찍고 비용이 들지 않습니다 — 포털까지 가지 않고 모양만 볼 때 씁니다.
- 프롬프트 · 응답 본문은 기록하지 않습니다(`enable_content_recording=False`). 켜는 인자를 두지 않은 것은
  이 랩이 공용 Application Insights 로 내보내기 때문입니다.
- **exporter 를 먼저 세우고 `instrument()` 를 나중에** 호출합니다.
  OpenTelemetry 는 no-op provider 로 시작하고, 거기 넘긴 span 은 **오류 없이 사라지기** 때문입니다.
- 같은 이유로 `settings.tracing_implementation = "opentelemetry"` 와 `AZURE_EXPERIMENTAL_ENABLE_GENAI_TRACING=true` 가
  azure-core 클라이언트보다 먼저 와야 합니다. 빠뜨려도 실행은 멀쩡해 보이고, SDK 가 그리는 `create_agent` · `invoke_agent` span 만 없습니다.
- `researcher` → `analyst` → `writer` 가 **conversation 하나**를 공유합니다. 앞 agent 가 한 말은 대화가 들고 있으므로,
  뒤 agent 의 instructions 에 다시 적어 넣지 않습니다 — 그러면 이미 읽을 수 있는 것을 입력 토큰 주고 알려 주는 셈이 됩니다.
- root span 이 없으면 세 턴이 각자 trace 를 시작하고, 포털은 pipeline 하나가 아니라 무관한 호출 셋을 보여 줍니다.

```bash
# 기본 : Application Insights 로 보내고 agent 는 남기기
python hol-foundry-observability-multi.py \
  --endpoint "<foundry-project-endpoint>"

# 다른 과제로 돌리고, 끝나고 정리
python hol-foundry-observability-multi.py \
  --endpoint "<foundry-project-endpoint>" \
  --task "배포 후 주문 API 오류율이 0.2%에서 7%로 올랐다. 원인과 대응을 정리해줘." \
  --delete
```

---

### hol-foundry-observability-propagation.py

여기까지는 전부 한 프로세스 안이었고, trace 가 이어진 것은 OpenTelemetry 의 context 가 context variable 이라 그냥 상속됐기 때문입니다.
그건 전파(propagation)가 아닙니다. agent 를 **HTTP 로 부르는 순간** context 는 따라가지 않고, task 하나당 trace 하나가 아니라 서비스 하나당 trace 하나가 됩니다.

이 스크립트는 세 agent 를 백그라운드 스레드의 로컬 HTTP 서버로 서빙해서, 호출이 실제로 소켓을 건너게 만듭니다.
핸들러는 요청이 만든 스레드에서 **아무 context 없이** 시작하므로 실험이 성립합니다.

```mermaid
%%{init: {"theme": "neutral"}}%%
flowchart TB
    ROOT["Scenario: pipeline over HTTP (root span)"] --> P1["POST /researcher — inject(headers)"]
    ROOT --> P2["POST /analyst"]
    ROOT --> P3["POST /writer"]
    P1 --> H1["handle /researcher — extract(headers)"] --> I1["invoke_agent"]
    P2 --> H2["handle /analyst"] --> I2["invoke_agent"]
    P3 --> H3["handle /writer"] --> I3["invoke_agent"]
```

| 인자 | 기본값 | 설명 |
|---|---|---|
| `--endpoint` | (필수) | Foundry project 엔드포인트 |
| `--deployment` | `gpt-5.6-terra` | 모델 Deployment 이름 |
| `--export` | `azure-monitor` | `azure-monitor` · `console` |
| `--port` | `8099` | 로컬 agent 서버 포트 |
| `--no-propagate` | 끔 | trace 헤더를 보내지 않음 (대조군) |
| `--delete` | 끔 | 끝나고 agent 셋 정리 |

- 이 스크립트는 [`identity.py`](identity.py) 를 쓰지 않습니다. `DefaultAzureCredential` 고정이라
  위의 **공통 인증** 인자(`--auth` 등)가 없고, `az login` 만 해 두면 됩니다.
- `--export azure-monitor` 는 project 에 연결된 Application Insights 로 보내고, 그때서야 **Foundry 포털 > Traces** 에 뜹니다 — 2~5분 걸립니다.
  연결 문자열은 project 에게 물어보므로(`project.telemetry.get_application_insights_connection_string()`) 따로 복사해 둘 것이 없고,
  Application Insights 가 연결되지 않은 project 에서는 이 지점에서 실패합니다.
  다만 **판정만 볼 거라면 `--export console` 이 빠릅니다** — hop 이 붙었는지 갈라졌는지는 아래 판정 줄로 바로 나옵니다.
- 프롬프트 · 응답 본문은 기록하지 않습니다. 판정에 필요한 것은 trace id 와 부모 span id 뿐입니다.
- **exporter 를 먼저 세우고 `instrument()` 를 나중에** 호출합니다.
  OpenTelemetry 는 no-op provider 로 시작하고, 거기 넘긴 span 은 **오류 없이 사라지기** 때문입니다.
- 같은 이유로 `settings.tracing_implementation = "opentelemetry"` 와 `AZURE_EXPERIMENTAL_ENABLE_GENAI_TRACING=true` 가
  azure-core 클라이언트보다 먼저 와야 합니다. 빠뜨려도 실행은 멀쩡해 보이고, SDK 가 그리는 `create_agent` · `invoke_agent` span 만 없습니다.
- **그대로 한 번, `--no-propagate` 로 한 번** 실행해 보세요. 끝에 찍히는 판정이 `joined` ↔ `SEPARATE` 로 뒤집힙니다.
- 서버는 헤더 키를 **소문자로 낮춘 뒤** `extract()` 에 넘깁니다. HTTP 헤더 이름은 대소문자를 가리지 않고 urllib 은 `Traceparent` 로 보내지만,
  기본 getter 는 dict 에서 소문자 이름을 그대로 찾습니다. 받은 그대로 넘기면 **아무 오류 없이** 못 찾고, 매 hop 이 자기 trace 를 시작합니다.
- `session.id` 를 baggage 에 한 번 담아 두면 손으로 넘기지 않아도 모든 서비스에 도착합니다.
- `--export azure-monitor` 는 `sampling_ratio=1.0` 으로 고정합니다. 샘플러가 trace 를 버리면 단지 안 보이는 것으로 끝나지 않고,
  span 이 `NonRecordingSpan` 이 되면서 SDK 계측기가 `.attributes` 를 읽다 죽습니다.
- 이것은 `AZURE_TRACING_GEN_AI_*` 전파와 다릅니다 — 그건 SDK 가 Foundry 로 보내는 요청에만 도장을 찍습니다.
  **내 서비스 사이**를 잇는 것은 `inject` · `extract` 이고, 그 밖의 값을 나르는 것은 baggage 입니다.

```bash
# 전파 켬 : 세 hop 이 하나의 trace 로
python hol-foundry-observability-propagation.py \
  --endpoint "<foundry-project-endpoint>"

# 대조군 : 헤더를 빼면 hop 마다 trace 가 갈라진다
python hol-foundry-observability-propagation.py \
  --endpoint "<foundry-project-endpoint>" \
  --no-propagate
```

---

### hol-foundry-observability-af-single.py

[`hol-foundry-observability-single.py`](#hol-foundry-observability-singlepy) 와 같은 실행을 Agent Framework 로 쓴 것입니다.
달라지는 것은 **tool loop 를 누가 도느냐** 입니다. 여기서는 `agent.run()` 이 그 loop 를 소유하므로,
질문 하나가 `invoke_agent` span 하나가 되고 tool span 이 그 **자식**으로 들어갑니다.

```mermaid
%%{init: {"theme": "neutral"}}%%
flowchart TB
    ROOT["Scenario: single agent (root span)"] --> INV["invoke_agent weather-agent"]
    INV --> CH1["chat — 모델 왕복 1"]
    INV --> T["execute_tool fetch_weather"]
    INV --> CH2["chat — 모델 왕복 2"]
```

| 인자 | 기본값 | 설명 |
|---|---|---|
| `--endpoint` | (필수) | Foundry project 엔드포인트 |
| `--deployment` | `gpt-5.6-terra` | 모델 Deployment 이름 |
| `--export` | `azure-monitor` | `azure-monitor` · `console` |
| `--question` | `What is the weather in Seoul, and should I take an umbrella?` | 질문 |

- 이 스크립트는 [`identity.py`](identity.py) 를 쓰지 않습니다. `DefaultAzureCredential` 고정이라
  위의 **공통 인증** 인자(`--auth` 등)가 없고, `az login` 만 해 두면 됩니다.
- `--export azure-monitor` 는 project 에 연결된 Application Insights 로 보내고 2~5분 뒤 보입니다.
  연결 문자열은 project 에게 물어보므로(`project.telemetry.get_application_insights_connection_string()`) 따로 복사해 둘 것이 없고,
  Application Insights 가 연결되지 않은 project 에서는 이 지점에서 실패합니다.
  다만 이때 올라가는 것은 **이 프로세스의 span** 입니다 — 아래 정리 항목의 이유로 포털 Traces 에서 agent 행에 걸리지는 않습니다.
  `--export console` 은 span 을 터미널에 그대로 찍고 비용이 들지 않습니다.
- 프롬프트 · 응답 본문은 기록하지 않습니다(`enable_sensitive_data=False`). 켜는 인자를 두지 않은 것은
  이 랩이 공용 Application Insights 로 내보내기 때문입니다.
- 트레이싱 설정은 **무엇이 추적되기 전에 한 번** 끝나야 합니다. raw SDK 쪽처럼 `instrument()` 를 손으로 부르지는 않지만,
  provider 가 없는 상태에서 만들어진 span 이 조용히 사라지는 것은 똑같습니다.
- 계측기를 세우는 코드가 없습니다. provider 만 있으면 Agent Framework 가 스스로 추적하고, `configure_otel_providers()` 가 그 provider 를 만듭니다.
  `--export azure-monitor` 일 때는 Azure Monitor 가 provider 를 세우므로 프레임워크는 `enable_instrumentation()` 만 부릅니다 —
  signal 당 provider 는 하나뿐이라 둘이 함께 세울 수 없습니다.
- tool 스키마는 함수 시그니처 · `Field` 설명 · docstring 에서 만들어집니다. 함수와 따로 관리할 JSON 스키마가 없습니다.
- `@tool(approval_mode="never_require")` 는 트레이싱과 무관합니다. 빼면 호출마다 승인을 묻는데, 지켜보는 사람이 없는 스크립트는 답할 수 없습니다.
- 정리할 것이 없어 `--delete` 가 없습니다. 프레임워크는 모델 Deployment 에 직접 말하므로 **Foundry agent 가 만들어지지 않고**,
  포털이 이 span 들을 걸어 둘 agent 행도 없습니다. 여기서 보이는 것은 이 프로세스의 span 입니다.
- 전부 async 라 `azure.identity.aio` 쪽 credential 을 씁니다.

```bash
# 기본
python hol-foundry-observability-af-single.py \
  --endpoint "<foundry-project-endpoint>"

# raw SDK 버전과 나란히 비교 — span 트리 모양이 다르다
python hol-foundry-observability-single.py --endpoint "<foundry-project-endpoint>"
python hol-foundry-observability-af-single.py --endpoint "<foundry-project-endpoint>"
```

