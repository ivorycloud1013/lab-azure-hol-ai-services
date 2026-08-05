# lab-azure-hol-ai-services

Azure AI Foundry를 **Public 망 / Private 망 / Private 망 + URL 화이트리스트**로 나눠 배포하고,
세 망의 접근 통제 차이를 직접 비교해 보는 핸즈온 랩(HOL)이다.
인증은 셋 다 **keyless(Entra ID + RBAC)** 로 구성한다.

## 3개의 독립 시스템

각 스택은 **자기 리소스 그룹만 만들고, 자기 IaC 루트를 가지며, 따로 배포·갱신·삭제된다.**
**어떤 스택도 다른 스택을 참조하지 않아 배포 순서 제약이 없다.**

| 스택 | 리소스 그룹 | 소유하는 것 | 의존 |
|---|---|---|---|
| [`iac/public/`](iac/public/) | `rg-<RGBASENAME>-public` | Public VNet, Foundry(공용 + IP 화이트리스트), 모델, RBAC | 없음 |
| [`iac/private/`](iac/private/) | `rg-<RGBASENAME>-private` | VNet, NSG, Bastion, 점프박스, Foundry(비공개) + PE + DNS | 없음 |
| [`iac/private-whitelist/`](iac/private-whitelist/) | `rg-<RGBASENAME>-private-whitelist` | private과 같은 한 벌 + UDR + Firewall Policy + Azure Firewall | 없음 |

스택 2와 3은 **같은 구성에 URL 통제만 다른 두 시스템**이다. 나란히 띄워 놓고
같은 명령을 두 점프박스에서 실행하면 화이트리스트의 효과가 그대로 드러난다.
공통 구성(NSG deny-all, 비공개 Foundry, Bastion, 점프박스, keyless RBAC)은
`iac/modules/workload/private-foundry-workload.bicep` 한 곳에서 공유한다.

## 통제 방식 비교

| | Public 망 | Private 망 | Private + 화이트리스트 |
|---|---|---|---|
| 접근 경로 | 노트북 → 인터넷 | 노트북 → Bastion → 점프박스 → PE | 좌동 |
| Foundry 노출 | `Enabled` + IP 화이트리스트 | `Disabled` | `Disabled` |
| Azure 서비스 우회 | `bypass=AzureServices` | `bypass=None` | `bypass=None` |
| **아웃바운드 URL** | 제한 없음 | **제한 없음**(NSG의 대역·포트까지만) | **FQDN 화이트리스트**(UDR → Azure Firewall) |
| NSG | 모든 서브넷 + deny-all 기본 | 모든 서브넷 + deny-all 기본 | 모든 서브넷 + deny-all 기본 |
| 인증 | keyless (`disableLocalAuth=true`) | keyless | keyless |

## 빠른 시작

```bash
az login
export RGBASENAME=hol01 REGION=westus3

# 1) Public
az deployment sub create -n $RGBASENAME-public -l $REGION --template-file iac/public/main.bicep \
  --parameters resourceGroupBaseName=$RGBASENAME location=$REGION \
               labClientIpAddress="$(curl -s ifconfig.me)" \
               labUserPrincipalId="$(az ad signed-in-user show --query id -o tsv)"

# 2) Private (URL 통제 없음)
az deployment sub create -n $RGBASENAME-private -l $REGION --template-file iac/private/main.bicep \
  --parameters resourceGroupBaseName=$RGBASENAME location=$REGION \
               labUserPrincipalId="$(az ad signed-in-user show --query id -o tsv)" \
               vmAdminPassword='<12자 이상 복잡한 비밀번호>'

# 3) Private + URL 화이트리스트
az deployment sub create -n $RGBASENAME-private-whitelist -l $REGION \
  --template-file iac/private-whitelist/main.bicep \
  --parameters resourceGroupBaseName=$RGBASENAME location=$REGION \
               labUserPrincipalId="$(az ad signed-in-user show --query id -o tsv)" \
               vmAdminPassword='<12자 이상 복잡한 비밀번호>'
```

각 스택 디렉터리는 독립된 azd 프로젝트이기도 하다 (`cd iac/private && azd up`).

기본 리전은 **westus3**, 기본 모델은 **gpt-5.4-mini** 다.
(`gpt-4.x` 계열은 전 리전 Deprecating 상태라 신규 배포가 거부된다.)

VNet 대역은 서로 겹치지 않게 잡혀 있다 — public `10.10.0.0/16` · private `10.20.0.0/16` ·
private-whitelist `10.30.0.0/16`.

## 문서

- **[iac/README.md](iac/README.md)** — 아키텍처, 배포, 설계 결정과 제약, 실습 시나리오, 비용, 정리
- 스택별 상세: [public](iac/public/README.md) · [private](iac/private/README.md) · [private-whitelist](iac/private-whitelist/README.md)

## 비용 주의

Azure Firewall과 Bastion은 **유휴 상태에서도 시간당 과금**된다.
세 스택을 모두 켜 두면 약 **$1.17/시간**(private-whitelist ~$0.79 + private ~$0.38).
비교 실습이 끝나면 **private-whitelist 스택만 지워도** 시간당 $0.79가 절약된다.
끝나면 세 리소스 그룹을 모두 삭제한다.
