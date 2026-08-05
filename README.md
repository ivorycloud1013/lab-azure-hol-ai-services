# lab-azure-hol-ai-services

Azure AI Foundry를 세 가지 네트워크 구성으로 배포해 보고, 접근 통제 방식의 차이를 직접 비교하는
핸즈온 랩(HOL, Hands-on Lab)입니다.

세 구성 모두 **API 키를 사용하지 않습니다.** Entra ID로 발급받은 토큰과 RBAC 역할 할당으로만
Foundry를 호출합니다(keyless 인증).

## 세 개의 독립적인 스택

스택(stack)은 **한 번에 배포되는 단위**입니다. 이 저장소의 세 스택은 각자 자기 리소스 그룹만 만들고,
자기 Bicep 진입점(`main.bicep`)을 가지며, 서로의 리소스를 참조하지 않습니다.
따라서 **배포 순서를 지킬 필요가 없고, 하나를 삭제해도 나머지는 그대로 동작합니다.**

| 스택 | 리소스 그룹 | 만드는 리소스 | 다른 스택 의존 |
|---|---|---|---|
| [`iac/public/`](iac/public/) | `rg-<RGBASENAME>-public` | VNet, Foundry(공용 접근 + 허용 IP 제한), 모델 배포, 역할 할당 | 없음 |
| [`iac/private/`](iac/private/) | `rg-<RGBASENAME>-private` | VNet, NSG, Bastion, 점프박스 VM, Foundry(공용 접근 차단) + Private Endpoint + Private DNS | 없음 |
| [`iac/private-whitelist/`](iac/private-whitelist/) | `rg-<RGBASENAME>-private-whitelist` | private과 같은 리소스 + Route Table + Firewall Policy + Azure Firewall | 없음 |

`RGBASENAME`은 리소스 그룹 이름에 들어가는 값입니다. 예를 들어 `hol01`을 넘기면
`rg-hol01-public`, `rg-hol01-private`, `rg-hol01-private-whitelist` 세 개가 만들어집니다.

**스택 2와 스택 3은 구성이 같고, 아웃바운드(외부로 나가는 통신) 통제 방식만 다릅니다.**
두 스택을 같은 구독에 동시에 배포한 뒤 각 점프박스에서 같은 명령을 실행하면,
도메인 제한이 있을 때와 없을 때의 차이를 바로 확인할 수 있습니다.

두 스택이 공통으로 쓰는 리소스 구성은 `iac/modules/workload/private-foundry-workload.bicep`
한 파일에 모아 두었습니다. 같은 내용을 양쪽에 복사해 두면 한쪽만 수정했을 때
보안 설정이 서로 어긋나기 때문입니다.

## 통제 방식 비교

| | Public 망 | Private 망 | Private + 도메인 제한 |
|---|---|---|---|
| 접근 경로 | 노트북 → 인터넷 → Foundry | 노트북 → Bastion → 점프박스 VM → Private Endpoint → Foundry | 왼쪽과 동일 |
| Foundry 공용 엔드포인트 | 열림(`publicNetworkAccess=Enabled`)이지만 등록된 IP만 허용 | 닫힘(`Disabled`) | 닫힘(`Disabled`) |
| Azure 서비스 예외 통과 | 허용(`bypass=AzureServices`) | 차단(`bypass=None`) | 차단(`bypass=None`) |
| **아웃바운드 도메인 제한** | 없음 | **없음** — NSG가 IP 대역과 포트까지만 확인 | **있음** — Azure Firewall이 허용 도메인만 통과 |
| NSG | 모든 서브넷에 연결, 기본은 전부 차단 | 동일 | 동일 |
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

# 2) Private (아웃바운드 도메인 제한 없음)
az deployment sub create -n $RGBASENAME-private -l $REGION --template-file iac/private/main.bicep \
  --parameters resourceGroupBaseName=$RGBASENAME location=$REGION \
               labUserPrincipalId="$(az ad signed-in-user show --query id -o tsv)" \
               vmAdminPassword='<12자 이상 복잡한 비밀번호>'

# 3) Private + 아웃바운드 도메인 제한
az deployment sub create -n $RGBASENAME-private-whitelist -l $REGION \
  --template-file iac/private-whitelist/main.bicep \
  --parameters resourceGroupBaseName=$RGBASENAME location=$REGION \
               labUserPrincipalId="$(az ad signed-in-user show --query id -o tsv)" \
               vmAdminPassword='<12자 이상 복잡한 비밀번호>'
```

각 스택 디렉터리는 Azure Developer CLI(azd) 프로젝트이기도 합니다. 해당 폴더로 이동해
`azd up`을 실행해도 됩니다.

기본 리전은 **westus3**, 기본 모델은 **gpt-5.4-mini** 입니다.
(`gpt-4.x` 계열 모델은 전 리전에서 지원 종료 예정 상태라 신규 배포가 거부됩니다.)

VNet 주소 대역은 서로 겹치지 않게 나눠 두었습니다 — public `10.10.0.0/16`,
private `10.20.0.0/16`, private-whitelist `10.30.0.0/16`.

## 문서

- **[iac/README.md](iac/README.md)** — 아키텍처, 배포 방법, 설계 결정과 이유, 실습 시나리오, 비용, 리소스 정리
- 스택별 상세: [public](iac/public/README.md) · [private](iac/private/README.md) · [private-whitelist](iac/private-whitelist/README.md)

## 비용 주의

Azure Firewall과 Azure Bastion은 **사용하지 않고 켜 두기만 해도 시간당 요금이 부과됩니다.**
세 스택을 모두 배포해 두면 약 **시간당 $1.17**(private-whitelist 약 $0.79 + private 약 $0.38)입니다.

비교 실습이 끝났다면 **private-whitelist 스택만 삭제해도 시간당 약 $0.79가 절약됩니다.**
실습을 마치면 세 리소스 그룹을 모두 삭제하세요.
