# Run and deploy the Foundry hosted agent (hol-foundry-agents-hosted.py) with azd.
#
#   az login && azd auth login
#   azd ext install microsoft.foundry
#   export AZURE_AI_PROJECT_ENDPOINT=https://<resource>.ai.azure.com/api/projects/<project>
#   export AZURE_AI_MODEL_DEPLOYMENT_NAME=gpt-5.6-terra
#
#   make ask-local FILE="..." QUESTION="…"  # one answer on this machine
#   make local     FILE="..."               # serve it instead, nothing deployed
#   make deploy    FILE="..."               # ship the same file to Foundry
#   make invoke    QUESTION="…"
#
# The Foundry project and the model deployment come from iac/, so nothing here
# provisions them — azd only builds and hosts the agent.

FILE                          ?=
QUESTION                      ?= 이 문서는 무엇에 대한 것인가요?
PORT                          ?= 8088
AZURE_AI_MODEL_DEPLOYMENT_NAME ?= gpt-5.6-terra
STAGED                        := assets/document.md

HERE := $(patsubst %/,%,$(dir $(realpath $(lastword $(MAKEFILE_LIST)))))
# azd hooks inherit the shell environment, which is how FILE reaches prepackage.
AZD  := cd $(HERE) && AGENT_SOURCE_DOCUMENT="$(FILE)" \
        AZURE_AI_MODEL_DEPLOYMENT_NAME="$(AZURE_AI_MODEL_DEPLOYMENT_NAME)" azd

.PHONY: help ask-local local stage provision deploy invoke monitor down check-file check-endpoint

help:
	@echo "ask-local  FILE=…  ask the hosted agent's code one question, no server"
	@echo "local      FILE=…  run the agent on localhost:$(PORT), nothing deployed"
	@echo "stage      FILE=…  stage $(STAGED) and list exactly what deploy uploads"
	@echo "provision          create the azd environment against the existing project"
	@echo "deploy     FILE=…  package and deploy to Foundry Agent Service"
	@echo "invoke     QUESTION=…  ask the deployed agent"
	@echo "monitor            follow the hosted agent's container logs"
	@echo "down               remove what azd created"

# Same code path the container runs, minus the server — the fast way to see the
# agent's tools work before spending a deploy on it.
ask-local: check-file check-endpoint
	cd $(HERE) && python hol-foundry-agents-hosted.py --file "$(FILE)" --question "$(QUESTION)"

local: check-file check-endpoint
	cd $(HERE) && python hol-foundry-agents-hosted.py --file "$(FILE)" --port $(PORT)

# azd itself runs the prepackage hook and applies .agentignore, so asking azd to
# package is the only way to see the real upload. Anything else is a guess.
stage: check-file check-endpoint
	$(AZD) package
	@echo "--- upload ---" && unzip -l "$$(ls -t $(TMPDIR)azd-code-deploy-*.zip | head -1)" | sed -n '4,$$p'

provision: check-endpoint
	$(AZD) provision

deploy: check-file check-endpoint
	$(AZD) deploy

invoke:
	$(AZD) ai agent invoke "$(QUESTION)"

monitor:
	$(AZD) ai agent monitor --follow

down:
	$(AZD) down

check-file:
	@test -n "$(FILE)" || { echo "pass FILE=<markdown file>, e.g. make $(MAKECMDGOALS) FILE=assets/doc.md"; exit 1; }
	@test -f "$(HERE)/$(FILE)" || test -f "$(FILE)" || { echo "$(FILE) not found"; exit 1; }

check-endpoint:
	@test -n "$(AZURE_AI_PROJECT_ENDPOINT)" || \
		{ echo "set AZURE_AI_PROJECT_ENDPOINT to the project endpoint"; exit 1; }
