# lab-azure-hol-ai-services — IaC 배포 Makefile
#
# 세 시스템은 서로 의존하지 않으므로 원하는 것만 따로 배포하고 따로 삭제할 수 있다.
# 모든 배포는 구독 범위(subscription scope)이며, 리소스 그룹은 템플릿이 직접 만든다.
#
#   make deploy-public
#   make deploy-private
#   make deploy-private-whitelist
#
# 값은 make 인자나 환경 변수로 덮어쓴다.
#
#   make deploy-public RGBASENAME=hol02 REGION=eastus2
#
# `az login` 은 이미 로그인돼 있으면 자동으로 건너뛴다.

SHELL := /bin/bash
.DEFAULT_GOAL := help

# ---------------------------------------------------------------------------
# 사용자가 덮어쓰는 값
# ---------------------------------------------------------------------------

# 리소스 그룹 이름에 들어가는 값. rg-$(RGBASENAME)-public 형태가 된다.
RGBASENAME ?= hol01

# 배포 리전. 기본 모델의 GA 가용성이 확인된 리전만 허용된다.
REGION ?= westus3

# 비워 두면 배포 직전에 자동으로 조회한다.
#   CLIENT_IP    — 실습자 노트북 공인 IP (public 전용, IP 화이트리스트에 등록)
#   PRINCIPAL_ID — 실습자 Entra 오브젝트 ID (keyless 접근용 RBAC 역할 할당)
CLIENT_IP    ?=
PRINCIPAL_ID ?=

# 점프박스 VM 비밀번호(private · private-whitelist 전용). 12자 이상 복잡도 요구.
# 비워 두면 배포 직전에 화면에 표시하지 않고 입력받는다.
# 셸 히스토리에 남기지 않으려면 인자로 넘기지 말고 입력 프롬프트를 쓰는 편이 낫다.
VM_ADMIN_PASSWORD ?=

# 점프박스 VM 크기. 비우면 템플릿 기본값(Standard_D2s_v5)을 쓴다.
# 리전에 따라 특정 크기의 용량이 부족해 SkuNotAvailable 로 실패할 수 있다.
# 그때는 make vm-sizes 로 쓸 수 있는 크기를 확인해 지정한다.
VM_SIZE ?=

# 삭제 타겟의 안전장치. destroy-* 는 CONFIRM=yes 없이는 동작하지 않는다.
CONFIRM ?=

# ---------------------------------------------------------------------------
# 내부 값
# ---------------------------------------------------------------------------

IP_LOOKUP_URL := https://ifconfig.me
SYSTEMS       := public private private-whitelist

# VM_SIZE 가 비어 있으면 매개변수 자체를 넘기지 않아 템플릿 기본값이 쓰인다.
VM_SIZE_PARAM = $(if $(VM_SIZE),vmSize=$(VM_SIZE))

# 공인 IP 를 $$CLIENT_IP 에 채운다.
define resolve_client_ip
CLIENT_IP="$(CLIENT_IP)"; \
if [ -z "$$CLIENT_IP" ]; then \
  CLIENT_IP=$$(curl -fsS --max-time 10 $(IP_LOOKUP_URL) || true); \
fi; \
if [ -z "$$CLIENT_IP" ]; then \
  echo "ERROR: 공인 IP 를 확인하지 못했습니다. CLIENT_IP=1.2.3.4 로 직접 지정하세요."; \
  exit 1; \
fi
endef

# 로그인한 사용자의 Entra 오브젝트 ID 를 $$PRINCIPAL_ID 에 채운다.
define resolve_principal_id
PRINCIPAL_ID="$(PRINCIPAL_ID)"; \
if [ -z "$$PRINCIPAL_ID" ]; then \
  PRINCIPAL_ID=$$(az ad signed-in-user show --query id -o tsv 2>/dev/null || true); \
fi; \
if [ -z "$$PRINCIPAL_ID" ]; then \
  echo "ERROR: Entra 오브젝트 ID 를 확인하지 못했습니다. PRINCIPAL_ID=<objectId> 로 직접 지정하세요."; \
  echo "       서비스 주체로 로그인한 경우 az ad signed-in-user 가 동작하지 않습니다."; \
  exit 1; \
fi
endef

# VM 비밀번호를 $$VM_PASSWORD 에 채운다. 인자로 넘어오지 않았으면 입력받는다.
#
# Azure VM 은 길이(12~72자)뿐 아니라 문자 종류 복잡성도 요구한다.
# 대문자 · 소문자 · 숫자 · 특수문자 중 3가지 이상을 포함해야 한다.
# Bicep 의 @minLength 로는 길이만 검사할 수 있어서 여기서 미리 걸러낸다.
# 배포가 몇 분 진행된 뒤 osProfile.adminPassword 에서 실패하는 것을 막기 위함이다.
define resolve_vm_password
VM_PASSWORD="$(VM_ADMIN_PASSWORD)"; \
if [ -z "$$VM_PASSWORD" ]; then \
  read -rsp "점프박스 VM 비밀번호(12자 이상, 대소문자·숫자·특수문자 중 3종류 이상): " VM_PASSWORD; echo; \
  read -rsp "확인을 위해 한 번 더 입력하세요: " VM_PASSWORD_CONFIRM; echo; \
  if [ "$$VM_PASSWORD" != "$$VM_PASSWORD_CONFIRM" ]; then \
    echo "ERROR: 두 번 입력한 비밀번호가 서로 다릅니다."; \
    echo "       화면에 표시되지 않으므로 오타를 확인하고 다시 실행하세요."; \
    exit 1; \
  fi; \
  unset VM_PASSWORD_CONFIRM; \
fi; \
if [ $${#VM_PASSWORD} -lt 12 ] || [ $${#VM_PASSWORD} -gt 72 ]; then \
  echo "ERROR: 비밀번호는 12~72자여야 합니다. (현재 $${#VM_PASSWORD}자)"; \
  exit 1; \
fi; \
CLASS_COUNT=0; \
case "$$VM_PASSWORD" in *[[:upper:]]*) CLASS_COUNT=$$((CLASS_COUNT + 1));; esac; \
case "$$VM_PASSWORD" in *[[:lower:]]*) CLASS_COUNT=$$((CLASS_COUNT + 1));; esac; \
case "$$VM_PASSWORD" in *[[:digit:]]*) CLASS_COUNT=$$((CLASS_COUNT + 1));; esac; \
case "$$VM_PASSWORD" in *[^[:alnum:]]*) CLASS_COUNT=$$((CLASS_COUNT + 1));; esac; \
if [ $$CLASS_COUNT -lt 3 ]; then \
  echo "ERROR: 비밀번호는 대문자 · 소문자 · 숫자 · 특수문자 중 3종류 이상을 포함해야 합니다."; \
  echo "       현재 $$CLASS_COUNT 종류만 들어 있습니다."; \
  echo "       VM_ADMIN_PASSWORD 를 인자로 넘겼다면 \$$ 가 make 에 의해 치환됐을 수 있습니다."; \
  echo "       인자 없이 실행해 입력 프롬프트를 쓰는 편이 안전합니다."; \
  exit 1; \
fi
endef

# ---------------------------------------------------------------------------
# 도움말
# ---------------------------------------------------------------------------

.PHONY: help
help:
	@echo "lab-azure-hol-ai-services — IaC 배포"
	@echo
	@echo "  배포"
	@echo "    make deploy-public                시스템 1 — Public 망 (IP 화이트리스트)"
	@echo "    make deploy-private               시스템 2 — Private 망 (아웃바운드 제한 없음)"
	@echo "    make deploy-private-whitelist     시스템 3 — Private 망 + 아웃바운드 도메인 제한"
	@echo
	@echo "  미리보기 (배포하지 않고 변경 사항만 계산)"
	@echo "    make whatif-public / whatif-private / whatif-private-whitelist"
	@echo
	@echo "  확인"
	@echo "    make verify-public / verify-private / verify-private-whitelist"
	@echo "    make outputs-public / outputs-private / outputs-private-whitelist"
	@echo
	@echo "  삭제 (리소스 그룹 통째로, CONFIRM=yes 필요)"
	@echo "    make destroy-public CONFIRM=yes"
	@echo
	@echo "  주요 변수"
	@echo "    RGBASENAME=$(RGBASENAME)          리소스 그룹 이름의 기준값"
	@echo "    REGION=$(REGION)             배포 리전"
	@echo "    CLIENT_IP                     비우면 $(IP_LOOKUP_URL) 로 자동 조회"
	@echo "    PRINCIPAL_ID                  비우면 로그인 사용자로 자동 조회"
	@echo "    VM_ADMIN_PASSWORD             비우면 입력 프롬프트 (private 계열 전용)"

# ---------------------------------------------------------------------------
# 로그인 — 이미 로그인돼 있으면 건너뛴다
# ---------------------------------------------------------------------------

.PHONY: login
login:
	@if az account show >/dev/null 2>&1; then \
	  echo "이미 로그인됨 — 구독: $$(az account show --query name -o tsv)"; \
	else \
	  echo "로그인이 필요합니다. az login 을 실행합니다."; \
	  az login; \
	fi

# ---------------------------------------------------------------------------
# 시스템 1 — Public 망
# ---------------------------------------------------------------------------

.PHONY: deploy-public
deploy-public: login
	@$(resolve_client_ip); \
	$(resolve_principal_id); \
	echo "배포: rg-$(RGBASENAME)-public ($(REGION)) · 허용 IP: $$CLIENT_IP"; \
	az deployment sub create \
	  --name $(RGBASENAME)-public \
	  --location $(REGION) \
	  --template-file public/main.bicep \
	  --parameters resourceGroupBaseName=$(RGBASENAME) \
	               location=$(REGION) \
	               labClientIpAddress="$$CLIENT_IP" \
	               labUserPrincipalId="$$PRINCIPAL_ID"

.PHONY: whatif-public
whatif-public: login
	@$(resolve_client_ip); \
	$(resolve_principal_id); \
	az deployment sub what-if \
	  --name $(RGBASENAME)-public \
	  --location $(REGION) \
	  --template-file public/main.bicep \
	  --parameters resourceGroupBaseName=$(RGBASENAME) \
	               location=$(REGION) \
	               labClientIpAddress="$$CLIENT_IP" \
	               labUserPrincipalId="$$PRINCIPAL_ID"

# NSG 가 빠진 서브넷이 없어야 정상이다.
.PHONY: verify-public
verify-public:
	@echo "NSG 없는 서브넷 (비어 있어야 정상):"
	@az deployment sub show --name $(RGBASENAME)-public \
	  --query properties.outputs.SUBNETS_WITHOUT_NSG.value -o json
	@echo "Foundry 엔드포인트:"
	@az deployment sub show --name $(RGBASENAME)-public \
	  --query properties.outputs.PUBLIC_FOUNDRY_ENDPOINT.value -o tsv

# ---------------------------------------------------------------------------
# 시스템 2 — Private 망 (아웃바운드 도메인 제한 없음)
# ---------------------------------------------------------------------------

.PHONY: deploy-private
deploy-private: login
	@$(resolve_principal_id); \
	$(resolve_vm_password); \
	echo "배포: rg-$(RGBASENAME)-private ($(REGION))"; \
	az deployment sub create \
	  --name $(RGBASENAME)-private \
	  --location $(REGION) \
	  --template-file private/main.bicep \
	  --parameters resourceGroupBaseName=$(RGBASENAME) \
	               location=$(REGION) \
	               labUserPrincipalId="$$PRINCIPAL_ID" \
	               vmAdminPassword="$$VM_PASSWORD" \
	               $(VM_SIZE_PARAM)

.PHONY: whatif-private
whatif-private: login
	@$(resolve_principal_id); \
	$(resolve_vm_password); \
	az deployment sub what-if \
	  --name $(RGBASENAME)-private \
	  --location $(REGION) \
	  --template-file private/main.bicep \
	  --parameters resourceGroupBaseName=$(RGBASENAME) \
	               location=$(REGION) \
	               labUserPrincipalId="$$PRINCIPAL_ID" \
	               vmAdminPassword="$$VM_PASSWORD" \
	               $(VM_SIZE_PARAM)

.PHONY: verify-private
verify-private:
	@echo "NSG 없는 서브넷 (비어 있어야 정상):"
	@az deployment sub show --name $(RGBASENAME)-private \
	  --query properties.outputs.SUBNETS_WITHOUT_NSG.value -o json
	@echo "점프박스 / Bastion:"
	@az deployment sub show --name $(RGBASENAME)-private \
	  --query '{jumpbox:properties.outputs.JUMPBOX_NAME.value,bastion:properties.outputs.BASTION_NAME.value}' -o table

# ---------------------------------------------------------------------------
# 시스템 3 — Private 망 + 아웃바운드 도메인 제한
# ---------------------------------------------------------------------------

.PHONY: deploy-private-whitelist
deploy-private-whitelist: login
	@$(resolve_principal_id); \
	$(resolve_vm_password); \
	echo "배포: rg-$(RGBASENAME)-private-whitelist ($(REGION))"; \
	az deployment sub create \
	  --name $(RGBASENAME)-private-whitelist \
	  --location $(REGION) \
	  --template-file private-whitelist/main.bicep \
	  --parameters resourceGroupBaseName=$(RGBASENAME) \
	               location=$(REGION) \
	               labUserPrincipalId="$$PRINCIPAL_ID" \
	               vmAdminPassword="$$VM_PASSWORD" \
	               $(VM_SIZE_PARAM)
	@$(MAKE) --no-print-directory verify-private-whitelist

.PHONY: whatif-private-whitelist
whatif-private-whitelist: login
	@$(resolve_principal_id); \
	$(resolve_vm_password); \
	az deployment sub what-if \
	  --name $(RGBASENAME)-private-whitelist \
	  --location $(REGION) \
	  --template-file private-whitelist/main.bicep \
	  --parameters resourceGroupBaseName=$(RGBASENAME) \
	               location=$(REGION) \
	               labUserPrincipalId="$$PRINCIPAL_ID" \
	               vmAdminPassword="$$VM_PASSWORD" \
	               $(VM_SIZE_PARAM)

# FIREWALL_ROUTE_IS_VALID 가 false 이면 Route Table 에 적어 둔 다음 홉 주소와
# 실제 방화벽 사설 IP 가 어긋난 상태다. 이 경우 아웃바운드가 방화벽을 우회한다.
.PHONY: verify-private-whitelist
verify-private-whitelist:
	@echo "방화벽 경로 정합성 (true 여야 정상):"
	@az deployment sub show --name $(RGBASENAME)-private-whitelist \
	  --query properties.outputs.FIREWALL_ROUTE_IS_VALID.value -o tsv
	@echo "NSG 없는 서브넷 (비어 있어야 정상):"
	@az deployment sub show --name $(RGBASENAME)-private-whitelist \
	  --query properties.outputs.SUBNETS_WITHOUT_NSG.value -o json
	@echo "허용 FQDN 개수:"
	@az deployment sub show --name $(RGBASENAME)-private-whitelist \
	  --query properties.outputs.ALLOWED_FQDN_COUNT.value -o tsv

# ---------------------------------------------------------------------------
# 리전에서 쓸 수 있는 VM 크기 확인
# ---------------------------------------------------------------------------

# SkuNotAvailable 로 배포가 실패하면 여기서 크기를 고른 뒤
# make deploy-private VM_SIZE=<크기> 로 다시 배포한다.
# restrictions 가 비어 있는 크기만 출력한다. 구독·리전마다 결과가 다르다.
.PHONY: vm-sizes
vm-sizes: login
	@echo "$(REGION) 에서 쓸 수 있는 2 vCPU D 계열 크기:"
	@az vm list-skus --location $(REGION) --resource-type virtualMachines \
	  --size Standard_D2 \
	  --query '[?length(restrictions) == `0`].name' -o tsv | sort

# ---------------------------------------------------------------------------
# 출력값 전체 보기
# ---------------------------------------------------------------------------

.PHONY: $(addprefix outputs-,$(SYSTEMS))
$(addprefix outputs-,$(SYSTEMS)): outputs-%:
	@az deployment sub show --name $(RGBASENAME)-$* \
	  --query properties.outputs -o json

# ---------------------------------------------------------------------------
# 삭제 — 리소스 그룹을 통째로 지운다
# ---------------------------------------------------------------------------

.PHONY: $(addprefix destroy-,$(SYSTEMS))
$(addprefix destroy-,$(SYSTEMS)): destroy-%: login
	@if [ "$(CONFIRM)" != "yes" ]; then \
	  echo "rg-$(RGBASENAME)-$* 를 통째로 삭제하려 합니다. 되돌릴 수 없습니다."; \
	  echo "실행하려면: make destroy-$* CONFIRM=yes"; \
	  exit 1; \
	fi
	az group delete --name rg-$(RGBASENAME)-$* --yes --no-wait
	@echo "삭제를 시작했습니다(백그라운드). 진행 상황: az group show -n rg-$(RGBASENAME)-$*"
