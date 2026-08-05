# 스택 3/3 — Private 망 + 아웃바운드 도메인 제한

**리소스 그룹:** `rg-<RGBASENAME>-private-whitelist` · **다른 스택 의존:** 없음 (언제든 단독으로 배포하고 삭제할 수 있습니다)

[스택 2 private](../private/)과 같은 구성(VNet + Foundry + VM)에,
**점프박스가 접속할 수 있는 도메인을 Azure Firewall로 제한하는 계층**을 추가한 스택입니다.

두 스택은 서로의 리소스를 참조하지 않습니다. 각자 자기 VNet, Foundry, VM을 갖고 있어서
따로 배포하고 따로 삭제할 수 있으며, 같은 구독에 둘 다 배포해 두면
**"도메인 제한이 없는 환경"과 "있는 환경"을 동시에 비교**할 수 있습니다.

| 항목 | 설정 |
|---|---|
| 인증 | API 키 없이 Entra ID 토큰만 사용(`disableLocalAuth=true`). 실습자 계정과 점프박스 VM의 관리 ID에 역할 할당 |
| 공용 엔드포인트 | 닫힘 (`publicNetworkAccess=Disabled`) |
| Azure 서비스 예외 통과 | 차단 (`networkAcls.bypass=None`) |
| 접근 경로 | Private Endpoint 만 (privatelink DNS 영역 3개 등록) |
| NSG | 모든 서브넷에 연결. 기본은 전부 차단하고 필요한 통신만 허용 |
| **아웃바운드** | **Route Table로 모든 트래픽을 Azure Firewall에 보내고, 허용 목록에 있는 도메인만 통과** |
| 거버넌스 | "모든 서브넷에 NSG를 연결해야 한다"를 검사하는 Azure Policy (기본값 `Audit`) |

## private 스택과 다른 점 — 세 가지

1. VNet 안에 방화벽용 서브넷(`AzureFirewallSubnet`, `AzureFirewallManagementSubnet`)을 함께 만듭니다
2. 점프박스 서브넷에 Route Table을 연결해, 외부로 나가는 모든 트래픽(`0.0.0.0/0`)을 방화벽으로 보냅니다
3. Firewall Policy(허용 도메인 목록)와 Azure Firewall을 배포합니다

그 외의 구성(NSG 기본 차단 규칙, 공용 접근을 막은 Foundry, Bastion, 점프박스 VM, 역할 할당)은
private 스택과 **같은 공통 모듈** `modules/workload/private-foundry-workload.bicep`을 사용합니다.
같은 내용을 양쪽에 복사해 두면 한쪽만 수정했을 때 보안 설정이 서로 어긋나기 때문입니다.

## 서브넷 구성 (`10.30.0.0/16` 기준)

| 서브넷 | 주소 대역 | NSG | Route Table |
|---|---|---|---|
| `AzureFirewallSubnet` | `10.30.0.0/26` | 연결 불가 (Azure 플랫폼 제약) | 없음 |
| `AzureFirewallManagementSubnet` | `10.30.0.64/26` | 연결 불가 (Azure 플랫폼 제약) | 없음 |
| `AzureBastionSubnet` | `10.30.0.128/26` | Bastion 필수 규칙 8개 + 기본 차단 | **연결하지 않음** (연결하면 Bastion 제어 트래픽이 끊깁니다) |
| `snet-private-endpoint` | `10.30.1.0/24` | 기본 차단 + 점프박스에서 오는 443 허용 | 없음 |
| `snet-jumpbox` | `10.30.2.0/24` | 기본 차단 + 필요한 통신만 허용 | `0.0.0.0/0 → 10.30.0.4` (방화벽) |

`AzureFirewallManagementSubnet`은 `firewallSkuTier=Basic`일 때만 만들어집니다.
SKU를 바꿔도 이 스택만 다시 배포하면 되므로, 다른 스택과 설정을 맞출 필요가 없습니다.

## 방화벽 규칙 구성

Azure Firewall은 규칙 컬렉션 그룹(rule collection group)을 우선순위 순서대로 평가합니다.

| 우선순위 | 규칙 그룹 | 내용 |
|---|---|---|
| 200 | `rcg-network-allow` | IP/포트 수준 허용. Azure 서비스 태그(Entra ID, Azure Resource Manager, Monitor 등) 기준 |
| 300 | `rcg-application-allow` | 도메인(FQDN) 수준 허용. Foundry, Azure Portal, 패키지 저장소, Windows Update |
| 350 | `rcg-application-allow-additional` | `additionalAllowedFqdns` 매개변수로 추가한 도메인 |
| 400 | `rcg-deny-all` | 나머지 전부 차단. 방화벽 기본 동작도 차단이지만, 로그에 남기려고 명시적으로 둡니다 |

## 배포

```bash
az deployment sub create -n hol01-private-whitelist -l westus3 \
  --template-file main.bicep \
  --parameters resourceGroupBaseName=hol01 location=westus3 \
               labUserPrincipalId="$(az ad signed-in-user show --query id -o tsv)" \
               vmAdminPassword='<12자 이상 복잡한 비밀번호>'
```

또는 이 디렉터리에서 `azd up`을 실행합니다.

## 허용 도메인 추가하기

매개변수만 바꿔 같은 스택을 다시 배포하면 됩니다.
방화벽과 정책만 갱신되고 VNet, VM, Foundry는 그대로 유지됩니다.

```bash
az deployment sub create -n hol01-private-whitelist -l westus3 \
  --template-file main.bicep \
  --parameters resourceGroupBaseName=hol01 location=westus3 \
               vmAdminPassword='<처음 배포할 때와 같은 비밀번호>' \
               additionalAllowedFqdns='["github.com","*.githubusercontent.com"]'
```

허용 목록 관련 매개변수: `identityAndManagementFqdns` / `foundryFqdns` / `portalFqdns` /
`toolingFqdns` / `additionalAllowedFqdns` / `allowedServiceTags` / `allowedFqdnTags`

## 주요 매개변수

| 매개변수 | 기본값 | 설명 |
|---|---|---|
| `vmAdminPassword` | (필수) | 12자 이상이며 Windows 암호 복잡성 요구사항을 만족해야 합니다 |
| `vnetAddressPrefix` | `10.30.0.0/16` | public(10.10.0.0/16), private(10.20.0.0/16)과 겹치면 안 됩니다 |
| `firewallSkuTier` | `Basic` | Basic은 관리용 서브넷과 공용 IP가 추가로 필요하며, 이 스택이 함께 만듭니다 |
| `threatIntelMode` | `Alert` | Basic SKU는 경고(Alert)까지만 지원합니다 |
| `deployLogAnalytics` | `true` | 무엇이 차단됐는지 확인할 수 없으면 실습이 성립하지 않으므로 기본값이 `true`입니다 |
| `subnetNsgPolicyEffect` | `Audit` | 처음에는 위반 사항을 기록만 합니다. 한 번 배포한 뒤 `Deny`로 올리는 것을 권장합니다 |

## 배포 후 확인

**`FIREWALL_ROUTE_IS_VALID` 출력값이 `true`여야 합니다.**

```bash
az deployment sub show -n hol01-private-whitelist \
  --query properties.outputs.FIREWALL_ROUTE_IS_VALID.value
```

`false`이면 실제 할당된 방화벽 IP가 Route Table에 설정한 주소와 다르다는 뜻입니다.
이 경우 점프박스의 트래픽이 존재하지 않는 주소로 전달되어 아웃바운드 통신이 모두 실패합니다.

- `SUBNETS_WITHOUT_NSG` 출력값에는 `AzureFirewallSubnet`과 `AzureFirewallManagementSubnet`
  **두 개만** 나와야 합니다. 이 두 서브넷은 Azure 플랫폼이 NSG 연결을 지원하지 않습니다.
- 점프박스 VM에서 도메인 제한이 동작하는지 확인합니다.

```powershell
Invoke-WebRequest https://login.microsoftonline.com  # 성공 (허용 목록에 있음)
Invoke-WebRequest https://github.com                 # 실패 (허용 목록에 없음)
```

- 차단 기록은 Log Analytics에서 조회합니다.

```kusto
AZFWApplicationRule
| where TimeGenerated > ago(30m)
| project TimeGenerated, SourceIp, Fqdn, Action, Rule
| order by TimeGenerated desc
```

## 주의 사항

- 이 스택은 **시간당 약 $0.79**의 요금이 발생합니다
  (Azure Firewall Basic + 공용 IP 2개 + Bastion + 점프박스 VM).
  실습을 쉬는 동안 이 스택만 삭제하면 요금이 바로 줄어들고, private 스택은 그대로 남습니다.
- Azure Firewall Basic SKU는 위협 인텔리전스 기능을 경고(`Alert`)까지만 지원합니다.

자세한 배경 설명은 [../README.md](../README.md)를 참고하세요.
