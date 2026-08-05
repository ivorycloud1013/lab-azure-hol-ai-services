# lab-azure-hol-ai-services

Azure AI Foundry를 **Public 망 / Private 망**으로 나눠 배포하고, 두 망의 접근 통제 차이를
직접 비교해 보는 핸즈온 랩(HOL)이다. 인증은 양쪽 모두 **keyless(Entra ID + RBAC)** 로 구성한다.

## 3개의 독립 시스템

각 스택은 **자기 리소스 그룹만 만들고, 자기 IaC 루트를 가지며, 따로 배포·갱신·삭제된다.**

| 스택 | 리소스 그룹 | 소유하는 것 | 의존 |
|---|---|---|---|
| [`iac/public/`](iac/public/) | `rg-<env>-public` | Public VNet, Foundry(공용 + IP 화이트리스트), 모델, RBAC | 없음 |
| [`iac/private/`](iac/private/) | `rg-<env>-private` | Private VNet, NSG, UDR, Bastion, 점프박스, Foundry(비공개) + PE + DNS | 없음 |
| [`iac/whitelist/`](iac/whitelist/) | `rg-<env>-whitelist` | Firewall Policy(FQDN 화이트리스트), Azure Firewall | `private` 먼저 |

스택 간 결합은 단 하나다 — Azure Firewall은 private 망 VNet의 `AzureFirewallSubnet`에 들어가야 하므로
배포 순서가 **private → whitelist** 다. public은 완전히 독립이다.

whitelist 스택을 지우면 같은 private 망이 **완전 차단 상태**로 돌아가고, 다시 배포하면 화이트리스트대로 뚫린다.
스택을 나눈 덕에 가능한 시연이다.

## 통제 방식 비교

| | Public 망 | Private 망 |
|---|---|---|
| 접근 경로 | 노트북 → 인터넷 | 노트북 → Bastion → 점프박스 VM → Private Endpoint |
| Foundry 노출 | `publicNetworkAccess=Enabled` + IP 화이트리스트 | `publicNetworkAccess=Disabled` |
| Azure 서비스 우회 | `bypass=AzureServices` | `bypass=None` (Azure도 차단) |
| 아웃바운드 | 제한 없음 | UDR 강제 터널링 → Azure Firewall FQDN 화이트리스트 |
| NSG | 모든 서브넷 + deny-all 기본 | 모든 서브넷 + deny-all 기본 |
| 인증 | keyless (`disableLocalAuth=true`) | keyless (`disableLocalAuth=true`) |

## 빠른 시작

```bash
az login
export ENV=hol01 LOC=westus3

# 1) Public (독립)
az deployment sub create -n $ENV-public -l $LOC --template-file iac/public/main.bicep \
  --parameters environmentName=$ENV location=$LOC \
               labClientIpAddress="$(curl -s ifconfig.me)" \
               labUserPrincipalId="$(az ad signed-in-user show --query id -o tsv)"

# 2) Private
az deployment sub create -n $ENV-private -l $LOC --template-file iac/private/main.bicep \
  --parameters environmentName=$ENV location=$LOC \
               labUserPrincipalId="$(az ad signed-in-user show --query id -o tsv)" \
               vmAdminPassword='<12자 이상 복잡한 비밀번호>'

# 3) Whitelist (private 출력을 입력으로)
PRV=$(az deployment sub show -n $ENV-private --query properties.outputs -o json)
az deployment sub create -n $ENV-whitelist -l $LOC --template-file iac/whitelist/main.bicep \
  --parameters environmentName=$ENV location=$LOC \
               privateVnetResourceGroupName=$(echo $PRV | jq -r .PRIVATE_RESOURCE_GROUP.value) \
               privateVnetName=$(echo $PRV | jq -r .PRIVATE_VNET_NAME.value) \
               expectedFirewallPrivateIp=$(echo $PRV | jq -r .EXPECTED_FIREWALL_PRIVATE_IP.value)
```

각 스택 디렉터리는 독립된 azd 프로젝트이기도 하다 (`cd iac/private && azd up`).

기본 리전은 **westus3**, 기본 모델은 **gpt-5.4-mini** 다.
(`gpt-4.x` 계열은 전 리전 Deprecating 상태라 신규 배포가 거부된다.)

## 문서

- **[iac/README.md](iac/README.md)** — 아키텍처, 배포 순서, 설계 결정과 제약, 실습 시나리오, 비용, 정리
- 스택별 상세: [public](iac/public/README.md) · [private](iac/private/README.md) · [whitelist](iac/whitelist/README.md)

## 비용 주의

Azure Firewall과 Bastion은 **유휴 상태에서도 시간당 과금**된다. 상시 과금 합계는 약 **$0.79/시간**
(whitelist ~$0.41 + private ~$0.38). 실습을 쉬는 동안 **whitelist 스택만 지워도** 시간당 $0.41이 절약된다.
끝나면 세 리소스 그룹을 모두 삭제한다.
