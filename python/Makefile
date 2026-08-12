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

# azd binds to an existing Foundry project through FOUNDRY_PROJECT_ENDPOINT and
# AZURE_AI_PROJECT_ID, not AZURE_AI_PROJECT_ENDPOINT. If either is missing,
# azd provision takes its greenfield path and creates a whole new Foundry
# account instead of using the project iac/ already built. Both are derived
# from the one endpoint the lab exports, so nothing else has to be set.
# Lazily assigned — the az lookup only runs when a target actually needs it.
ACCOUNT_NAME = $(shell echo "$(AZURE_AI_PROJECT_ENDPOINT)" | sed -E 's|^https://([^.]+)\..*|\1|')
PROJECT_NAME = $(notdir $(AZURE_AI_PROJECT_ENDPOINT))
ACCOUNT_ID   = $(shell az resource list --name "$(ACCOUNT_NAME)" \
                 --resource-type Microsoft.CognitiveServices/accounts \
                 --query "[0].id" -o tsv)
AZURE_AI_PROJECT_ID ?= $(ACCOUNT_ID)/projects/$(PROJECT_NAME)

HERE := $(patsubst %/,%,$(dir $(realpath $(lastword $(MAKEFILE_LIST)))))
# azd hooks inherit the shell environment, which is how FILE reaches prepackage.
AZD  := cd $(HERE) && AGENT_SOURCE_DOCUMENT="$(FILE)" \
        AZURE_AI_MODEL_DEPLOYMENT_NAME="$(AZURE_AI_MODEL_DEPLOYMENT_NAME)" azd

.PHONY: help ask-local local stage bind provision deploy invoke monitor down check-file check-endpoint

help:
	@echo "ask-local  FILE=…  ask the hosted agent's code one question, no server"
	@echo "local      FILE=…  run the agent on localhost:$(PORT), nothing deployed"
	@echo "stage      FILE=…  stage $(STAGED) and list exactly what deploy uploads"
	@echo "bind               point the azd environment at the existing Foundry project"
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

# Writes the binding values into the azd environment. Run before anything that
# talks to Azure, or azd will provision a second Foundry account of its own.
bind: check-endpoint
	@test -n "$(ACCOUNT_ID)" || { echo "could not find account $(ACCOUNT_NAME) — check az login and AZURE_AI_PROJECT_ENDPOINT"; exit 1; }
	$(AZD) env set FOUNDRY_PROJECT_ENDPOINT "$(AZURE_AI_PROJECT_ENDPOINT)"
	$(AZD) env set AZURE_AI_PROJECT_ENDPOINT "$(AZURE_AI_PROJECT_ENDPOINT)"
	$(AZD) env set AZURE_AI_PROJECT_ID "$(AZURE_AI_PROJECT_ID)"
	# azure.yaml resolves ${...} from the azd environment, not from the shell, so
	# exporting this variable is not enough — the container starts without a model
	# and crashes with "Model is required" until it is written here.
	$(AZD) env set AZURE_AI_MODEL_DEPLOYMENT_NAME "$(AZURE_AI_MODEL_DEPLOYMENT_NAME)"

provision: bind
	$(AZD) provision

deploy: check-file bind
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
