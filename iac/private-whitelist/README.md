# 스택 3/3 — Private 망 + URL 화이트리스트

**리소스 그룹:** `rg-<RGBASENAME>-private-whitelist` · **다른 스택 의존:** 없음 (언제든 단독 배포/삭제 가능)

[스택 2 private](../private/)과 같은 구성(VNet + Foundry + VM)에
**점프박스가 접근하는 URL을 Azure Firewall FQDN 화이트리스트로 통제하는 층**을 더한 스택이다.

두 스택은 서로를 참조하지 않는다. 각자 자기 VNet·Foundry·VM을 갖고 따로 배포·삭제되므로,
같은 구독에 나란히 띄워 **"URL 통제가 없는 망"과 "있는 망"을 동시에 비교**할 수 있다.

| 항목 | 설정 |
|---|---|
| 인증 | keyless — `disableLocalAuth=true`, 실습자 + 점프박스 관리 ID에 RBAC |
| 공용 접근 | `publicNetworkAccess=Disabled` |
| Azure 우회 | `networkAcls.bypass=None` |
| 접근 경로 | Private Endpoint 단독 (privatelink 존 3개) |
| NSG | 모든 서브넷에 연결, 우선순위 4096 deny-all 기준선 |
| **아웃바운드** | **UDR 강제 터널링 → Azure Firewall FQDN 화이트리스트** |
| 거버넌스 | "모든 서브넷 NSG 필수" Azure Policy (기본 `Audit`) |

## private 스택과의 차이 — 딱 세 가지

1. VNet에 `AzureFirewallSubnet`(+`AzureFirewallManagementSubnet`)을 함께 만든다
2. 점프박스 서브넷에 `0.0.0.0/0 → 방화벽` UDR을 건다
3. Firewall Policy(FQDN 화이트리스트)와 Azure Firewall을 배포한다

나머지(NSG deny-all 기준선, 비공개 Foundry, Bastion, 점프박스, keyless RBAC)는
private 스택과 **같은 공통 모듈** `modules/workload/private-foundry-workload.bicep` 을 쓴다.
두 스택이 각자 복제하면 보안 기준선이 따로 놀기 때문이다.

## 주소 배치 (`10.30.0.0/16` 기준)

| 서브넷 | CIDR | NSG | UDR |
|---|---|---|---|
| `AzureFirewallSubnet` | `10.30.0.0/26` | 불가(플랫폼 제약) | — |
| `AzureFirewallManagementSubnet` | `10.30.0.64/26` | 불가(플랫폼 제약) | — |
| `AzureBastionSubnet` | `10.30.0.128/26` | 필수 규칙 8개 + deny-all | **걸지 않음**(제어 평면 차단됨) |
| `snet-private-endpoint` | `10.30.1.0/24` | deny-all + 점프박스 443 | — |
| `snet-jumpbox` | `10.30.2.0/24` | deny-all + 최소 허용 | `0.0.0.0/0 → 10.30.0.4` |

`AzureFirewallManagementSubnet`은 `firewallSkuTier=Basic`일 때만 만들어진다.
SKU를 바꿔도 이 스택 하나만 다시 배포하면 되므로 다른 스택과 맞출 설정이 없다.

## 규칙 구조

| 우선순위 | 규칙 그룹 | 내용 |
|---|---|---|
| 200 | `rcg-network-allow` | L3/L4 서비스 태그 화이트리스트 (Entra ID, ARM, Monitor 등) |
| 300 | `rcg-application-allow` | L7 FQDN 화이트리스트 (Foundry, 포털, 패키지 저장소, Windows Update) |
| 350 | `rcg-application-allow-additional` | `additionalAllowedFqdns` 확장분 |
| 400 | `rcg-deny-all` | 명시적 Deny-All. 방화벽 기본도 deny지만 로그에 남기려고 둔다 |

## 배포

```bash
az deployment sub create -n hol01-private-whitelist -l westus3 \
  --template-file main.bicep \
  --parameters resourceGroupBaseName=hol01 location=westus3 \
               labUserPrincipalId="$(az ad signed-in-user show --query id -o tsv)" \
               vmAdminPassword='<12자 이상 복잡한 비밀번호>'
```

또는 이 디렉터리에서 `azd up`.

## 화이트리스트 갱신

규칙만 바꿔 같은 스택을 다시 밀면 된다. 방화벽·정책만 갱신되고 VNet/VM/Foundry는 그대로다.

```bash
az deployment sub create -n hol01-private-whitelist -l westus3 \
  --template-file main.bicep \
  --parameters resourceGroupBaseName=hol01 location=westus3 \
               vmAdminPassword='<같은 비밀번호>' \
               additionalAllowedFqdns='["github.com","*.githubusercontent.com"]'
```

허용 목록 매개변수: `identityAndManagementFqdns` / `foundryFqdns` / `portalFqdns` / `toolingFqdns` /
`additionalAllowedFqdns` / `allowedServiceTags` / `allowedFqdnTags`

## 주요 매개변수

| 매개변수 | 기본값 | 설명 |
|---|---|---|
| `vmAdminPassword` | (필수) | 12자 이상, Windows 복잡성 요구사항 충족 |
| `vnetAddressPrefix` | `10.30.0.0/16` | public(10.10) · private(10.20)와 겹치면 안 된다 |
| `firewallSkuTier` | `Basic` | Basic은 관리 서브넷 + 공인 IP를 이 스택이 함께 만든다 |
| `threatIntelMode` | `Alert` | Basic SKU는 `Alert`까지만 지원 |
| `deployLogAnalytics` | `true` | 무엇이 차단됐는지 못 보면 실습이 성립하지 않는다 |
| `subnetNsgPolicyEffect` | `Audit` | 한 번 배포한 뒤 `Deny`로 올리는 것을 권장 |

## 확인

**`FIREWALL_ROUTE_IS_VALID` 가 `true` 여야 한다.**

```bash
az deployment sub show -n hol01-private-whitelist \
  --query properties.outputs.FIREWALL_ROUTE_IS_VALID.value
```

`false`면 실제 방화벽 IP가 Route Table의 next hop(`cidrHost` 계산값)과 어긋나 강제 터널링이 동작하지 않는다.

- `SUBNETS_WITHOUT_NSG` 출력은 `["AzureFirewallSubnet","AzureFirewallManagementSubnet"]` **만** 나와야 한다.
- 점프박스에서 화이트리스트 체감:

```powershell
Invoke-WebRequest https://login.microsoftonline.com  # 허용 (화이트리스트)
Invoke-WebRequest https://github.com                 # 차단 (목록에 없음)
```

- 차단 로그:

```kusto
AZFWApplicationRule
| where TimeGenerated > ago(30m)
| project TimeGenerated, SourceIp, Fqdn, Action, Rule
| order by TimeGenerated desc
```

## 주의

- 이 스택은 시간당 약 **$0.79** 과금된다(Firewall Basic + 공인 IP 2개 + Bastion + 점프박스).
  실습을 쉴 때 이 스택만 지우면 즉시 절약된다 — private 스택은 그대로 남는다.
- Basic SKU는 위협 인텔리전스가 `Alert`까지만 지원한다.

자세한 배경은 [../README.md](../README.md) 참고.
