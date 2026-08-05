# IaC — Azure AI Foundry HOL 이중망 랜딩존

Azure AI Foundry를 **Public 망**과 **Private 망** 두 벌로 배포하고, 두 망의 접근 통제 차이를
실습으로 비교할 수 있게 만든 Bicep 템플릿이다. 인증은 양쪽 모두 **keyless(Entra ID + RBAC)** 다.

---

## 1. 아키텍처

```
                        ┌───────────────────────────────────────────────┐
   실습자 노트북 ──HTTPS─▶│ Public 망 (rg-<env>-public)                   │
        │                │                                               │
        │                │  Foundry (publicNetworkAccess=Enabled)        │
        │                │    networkAcls.defaultAction = Deny           │
        │                │    ├─ ipRules          = 노트북 공인 IP        │
        │                │    └─ virtualNetworkRules = snet-workload     │
        │                │  disableLocalAuth = true  (API 키 없음)        │
        │                │                                               │
        │                │  vnet 10.10.0.0/16                            │
        │                │    └ snet-workload 10.10.1.0/24 + NSG(deny-all)│
        │                └───────────────────────────────────────────────┘
        │
        │                ┌───────────────────────────────────────────────┐
        └──HTTPS(443)───▶│ Private 망 (rg-<env>-private)                 │
             Bastion     │                                               │
                         │  vnet 10.20.0.0/16                            │
                         │   ├ AzureFirewallSubnet        10.20.0.0/26   │  NSG 불가(플랫폼 예외)
                         │   ├ AzureFirewallManagementSub 10.20.0.64/26  │  NSG 불가(플랫폼 예외)
                         │   ├ AzureBastionSubnet         10.20.0.128/26 │  NSG + deny-all
                         │   ├ snet-private-endpoint      10.20.1.0/24   │  NSG + deny-all
                         │   └ snet-jumpbox               10.20.2.0/24   │  NSG + deny-all + UDR
                         │                                               │
                         │  Bastion ─▶ 점프박스 VM (공인 IP 없음)          │
                         │                │                              │
                         │                ├─▶ Private Endpoint ─▶ Foundry│
                         │                │      (publicNetworkAccess=Disabled)
                         │                │      (networkAcls.bypass=None)
                         │                │                              │
                         │                └─▶ 0.0.0.0/0 (UDR)            │
                         │                     ─▶ Azure Firewall         │
                         │                          FQDN 화이트리스트      │
                         │                          그 외 전부 Deny        │
                         └───────────────────────────────────────────────┘
```

### 3중 통제 (Private 망)

| 계층 | 수단 | 통제 내용 |
|---|---|---|
| L3/L4 | NSG | 모든 서브넷에 연결. 우선순위 **4096 deny-all**이 기본이고 명시 허용만 통과 |
| L7 | Azure Firewall | UDR로 강제 터널링. **FQDN 화이트리스트**만 허용, 나머지 명시적 Deny |
| 서비스 | Foundry 방화벽 | `publicNetworkAccess=Disabled` + `networkAcls.bypass=None`. Private Endpoint가 유일한 경로 |

`bypass=None` 이므로 "신뢰할 수 있는 Azure 서비스"조차 우회하지 못한다.
요구사항이었던 **"Azure도 막고 화이트리스트로만 뚫는다"** 를 구현한 부분이다.

---

## 2. 폴더 구조

```
iac/
├── main.bicep                 구독 범위 오케스트레이터. RG 3개 생성 후 존 모듈 호출
├── main.parameters.json       azd 파라미터 바인딩
└── modules/
    ├── network/
    │   ├── nsg.bicep                deny-all 기준선(4096) 자동 부착
    │   ├── vnet.bicep               subnetConfig 타입으로 NSG 필수화
    │   ├── route-table.bicep        0.0.0.0/0 → 방화벽 강제 터널링
    │   ├── firewall-policy.bicep    FQDN/서비스태그 화이트리스트 + 명시 Deny-All
    │   ├── firewall.bicep           Azure Firewall (Basic은 관리 서브넷 필요)
    │   ├── bastion.bicep            Azure Bastion
    │   ├── private-dns-zone.bicep   privatelink 존 + VNet 링크
    │   └── private-endpoint.bicep   PE + DNS Zone Group
    ├── ai/
    │   ├── foundry-account.bicep    keyless 계정 (disableLocalAuth + networkAcls)
    │   ├── foundry-project.bicep    Foundry 프로젝트
    │   └── model-deployments.bicep  모델 배포 (@batchSize(1) 순차)
    ├── compute/
    │   └── jumpbox.bicep            공인 IP 없는 Windows 점프박스 + 관리 ID
    ├── identity/
    │   ├── role-definitions.bicep   역할 GUID 상수
    │   └── foundry-role-assignments.bicep
    ├── monitor/
    │   └── log-analytics.bicep      방화벽/NSG/Foundry 진단 로그 수집
    ├── governance/
    │   ├── subnet-nsg-policy.bicep  "모든 서브넷 NSG 필수" 커스텀 정책
    │   └── policy-assignment.bicep  RG 범위 할당(스코프 분리용)
    └── zones/
        ├── public-zone.bicep        Public 망 조립
        └── private-zone.bicep       Private 망 조립
```

---

## 3. 사전 준비

```bash
az login
az account set --subscription <구독 ID>

# 노트북 공인 IP 확인 (Public Foundry 화이트리스트에 등록됨)
curl -s ifconfig.me
```

필요 권한: 구독 **Owner** 또는 (Contributor + User Access Administrator).
RBAC 역할 할당과 Azure Policy 정의를 만들기 때문이다.

---

## 4. 배포

### azd 사용 (권장)

```bash
azd auth login
azd env new hol01
azd env set LAB_CLIENT_IP        "$(curl -s ifconfig.me)"
azd env set VM_ADMIN_PASSWORD    '<12자 이상 복잡한 비밀번호>'
azd up
```

`labUserPrincipalId`는 azd가 `AZURE_PRINCIPAL_ID`로 로그인 사용자를 자동 주입한다.

### az CLI 사용

```bash
az deployment sub create \
  --name hol-deploy \
  --location westus3 \
  --template-file iac/main.bicep \
  --parameters environmentName=hol01 \
               location=westus3 \
               labClientIpAddress="$(curl -s ifconfig.me)" \
               labUserPrincipalId="$(az ad signed-in-user show --query id -o tsv)" \
               vmAdminPassword='<비밀번호>'
```

배포 전에 변경분만 미리 보려면:

```bash
az deployment sub what-if --location westus3 --template-file iac/main.bicep --parameters ...
```

### 주요 매개변수

| 매개변수 | 기본값 | 설명 |
|---|---|---|
| `location` | `westus3` | `westus3` / `eastus2` / `swedencentral` / `koreacentral`. 기본 모델의 GA 가용을 `az cognitiveservices model list`로 확인한 리전 |
| `firewallSkuTier` | `Basic` | Basic이 가장 저렴. Standard는 관리 서브넷 불필요 |
| `bastionSkuName` | `Basic` | Standard로 올리면 네이티브 클라이언트/터널링 사용 가능 |
| `deployPublicZone` / `deployPrivateZone` | `true` | 한쪽만 배포해 비용 절감 가능 |
| `deployLogAnalytics` | `true` | 방화벽 차단 로그 확인에 필요 |
| `subnetNsgPolicyEffect` | `Audit` | `Deny`로 올리면 NSG 없는 서브넷 생성 자체를 차단 |
| `additionalAllowedFqdns` | `[]` | 방화벽 화이트리스트 확장 |
| `modelDeployments` | gpt-5.4-mini 20 TPM | 배포할 모델 목록. 아래 "모델 선택" 주의사항 참고 |

---

## 5. 배포 직후 확인

```bash
az deployment sub show --name hol-deploy --query properties.outputs -o json
```

반드시 확인할 출력 세 개:

| 출력 | 기대값 | 의미 |
|---|---|---|
| `FIREWALL_ROUTE_IS_VALID` | `true` | UDR next hop이 실제 방화벽 IP와 일치. `false`면 강제 터널링이 끊긴 상태 |
| `PRIVATE_SUBNETS_WITHOUT_NSG` | `["AzureFirewallSubnet","AzureFirewallManagementSubnet"]` | 이 둘 외에 값이 있으면 NSG 누락 |
| `PUBLIC_SUBNETS_WITHOUT_NSG` | `[]` | 비어 있어야 정상 |

---

## 6. 실습 시나리오

### 6-1. Public 망 — keyless 호출 (노트북)

```python
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from openai import AzureOpenAI

token_provider = get_bearer_token_provider(
    DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
)
client = AzureOpenAI(
    azure_endpoint="<PUBLIC_FOUNDRY_ENDPOINT>",
    azure_ad_token_provider=token_provider,   # api_key 없음
    api_version="2024-10-21",
)
print(client.chat.completions.create(
    model="gpt-5.4-mini",
    messages=[{"role": "user", "content": "안녕"}],
).choices[0].message.content)
```

- **키가 아예 없다.** `disableLocalAuth=true` 이므로 `az cognitiveservices account keys list`도 실패한다.
- 노트북을 다른 네트워크(테더링 등)로 옮기면 IP 화이트리스트에서 벗어나 `403`이 난다.

### 6-2. Private 망 — 노트북에서는 실패해야 정상

같은 코드로 `PRIVATE_FOUNDRY_ENDPOINT`를 호출하면 실패한다.
`publicNetworkAccess=Disabled`라 공용 경로가 존재하지 않는다.

### 6-3. Private 망 — Bastion 경유 성공

```bash
az network bastion rdp --name <BASTION_NAME> --resource-group rg-<env>-private \
  --target-resource-id $(az vm show -g rg-<env>-private -n <JUMPBOX_NAME> --query id -o tsv)
```

점프박스에서 DNS를 확인하면 사설 IP로 해석된다.

```powershell
nslookup <private-foundry>.openai.azure.com     # 10.20.1.x 를 반환해야 정상
```

`DefaultAzureCredential()`이 VM 관리 ID를 자동으로 집어 키 없이 호출된다.

### 6-4. 방화벽 화이트리스트 체감

점프박스에서 허용/차단 대비를 확인한다.

```powershell
Invoke-WebRequest https://login.microsoftonline.com  # 허용 (화이트리스트)
Invoke-WebRequest https://github.com                 # 차단 (목록에 없음)
```

Log Analytics에서 차단 로그를 확인한다.

```kusto
AZFWApplicationRule
| where TimeGenerated > ago(30m)
| project TimeGenerated, SourceIp, Fqdn, Action, Rule
| order by TimeGenerated desc
```

FQDN을 열어주려면 재배포 없이 매개변수만 확장한다.

```bash
azd env set ADDITIONAL_ALLOWED_FQDNS '["github.com","*.githubusercontent.com"]'
```

### 6-5. NSG 거버넌스 확인

정책 준수 상태를 조회한다.

```bash
az policy state list --resource-group rg-<env>-private \
  --filter "policyDefinitionName eq 'policy-subnet-requires-nsg-<env>'" \
  --query "[].{res:resourceId, state:complianceState}" -o table
```

`subnetNsgPolicyEffect=Deny`로 올린 뒤 NSG 없는 서브넷 추가를 시도하면 거부된다.

---

## 7. 설계 결정과 제약

### NSG 예외는 방화벽 서브넷 두 개뿐

Azure는 `AzureFirewallSubnet` / `AzureFirewallManagementSubnet`에 **NSG 연결을 지원하지 않는다.**
"모든 서브넷에 NSG" 기준의 유일한 예외이며, `subnetsWithoutNsg` 출력으로 항상 드러나게 했다.
`AzureBastionSubnet`은 NSG가 **필수**이므로 문서화된 필수 규칙 8개를 모두 넣었다.

### NSG는 next hop이 아니라 원래 목적지로 평가된다

UDR로 0.0.0.0/0을 방화벽에 보내더라도, NSG는 패킷의 **원래 목적지**를 본다.
따라서 "방화벽 서브넷으로의 아웃바운드 허용"만으로는 인터넷 트래픽이 통과하지 못한다.
점프박스 NSG가 `Internet:80/443`을 L4에서 넓게 허용하고, 실제 FQDN 통제는 Azure Firewall이 맡는 이유다.
NSG는 대역·포트, 방화벽은 도메인 — 역할이 다르다.

### UDR next hop을 계산으로 구한 이유

`VNet → Firewall → RouteTable → VNet` 순환 의존을 피하기 위해,
방화벽 사설 IP를 `cidrHost(firewallSubnetPrefix, 3)`로 계산해 방화벽보다 **먼저** Route Table을 만든다.
Azure Firewall은 전용 서브넷에서 항상 첫 할당 가능 주소(`x.x.x.4`)를 받는다(`.0` 네트워크 / `.1` 게이트웨이 / `.2` `.3` 예약).
계산값과 실제값의 일치 여부는 `FIREWALL_ROUTE_IS_VALID` 출력으로 검증한다.

### AzureBastionSubnet에는 UDR을 걸지 않았다

`0.0.0.0/0`을 방화벽으로 보내면 Bastion 제어 평면이 끊겨 세션이 열리지 않는다. 의도적으로 제외했다.

### DNS는 Azure 제공 DNS를 사용한다

Azure Firewall DNS 프록시를 켜면 VNet DNS를 방화벽 IP로 바꿔야 해서 VNet을 두 번 배포해야 한다.
Private Endpoint 이름 해석은 VNet에 링크된 Private DNS Zone만으로 충분하고,
FQDN 필터링도 **애플리케이션 규칙**에서는 DNS 프록시 없이 동작하므로 켜지 않았다.
네트워크 규칙에 FQDN을 쓰려면 그때 DNS 프록시를 활성화한다.

### Private Endpoint 서브넷의 `privateEndpointNetworkPolicies=Enabled`

기본값은 `Disabled`(= PE 트래픽에 NSG 미적용)다. 여기서는 deny-all 기준을 PE에도 실제로 적용하기 위해
`Enabled`로 두고, 점프박스 서브넷에서의 443만 명시 허용했다.

### Azure Firewall Basic의 추가 요구사항

Basic SKU는 `AzureFirewallManagementSubnet`과 **별도 공인 IP**를 반드시 요구한다.
`firewallSkuTier=Standard`로 바꾸면 관리 서브넷이 자동으로 생략된다.

### 모델 선택 — `gpt-4.x` 계열은 쓸 수 없다

`az cognitiveservices model list`에 나온다고 배포 가능한 것이 아니다.
목록에는 **Deprecating 상태 모델도 함께 나오는데**, 이 모델들은 제어 평면 프리플라이트에서 거부된다.

```
ServiceModelDeprecating - The model 'Format:OpenAI,Name:gpt-4.1-mini,Version:2025-04-14'
is in deprecating state and cannot be used for new deployments.
```

`gpt-4o`, `gpt-4o-mini`, `gpt-4.1`, `gpt-4.1-mini`, `gpt-4.1-nano` 는 전부 Deprecating 이라 신규 배포가 불가능하다.
기본값을 `gpt-5.4-mini`(2026-03-17, GenerallyAvailable)로 잡은 이유다.

모델을 바꿀 때는 **반드시 `lifecycleStatus`를 먼저 확인한다.**

```bash
az cognitiveservices model list -l westus3 \
  --query "[?model.name=='<모델명>'].{v:model.version, ls:model.lifecycleStatus, s:join(',',model.skus[].name)}" -o table
```

`GenerallyAvailable` 이 아니면 배포되지 않는다.

### Log Analytics를 기본 배포하는 이유

요청 범위는 AI Foundry였지만, 방화벽 화이트리스트 실습은 **무엇이 차단됐는지 볼 수 없으면 성립하지 않는다.**
일일 수집 상한 1GB로 비용을 묶어 두었고, `deployLogAnalytics=false`로 끌 수 있다.

### `bicepconfig.json`에서 `no-hardcoded-env-urls`를 끈 이유

이 린터 규칙은 하드코딩된 Azure URL을 경고한다. 하지만 방화벽 FQDN 화이트리스트는
`login.microsoftonline.com` 같은 도메인을 **문자열 그대로 적는 것이 목적**이므로 규칙을 껐다.
대신 `no-unused-params` 등 실질적인 규칙은 `error`로 올렸다.

---

## 8. 비용

westus3 소매가 기준(USD, 2026-08 Azure Retail Prices API 조회값). 상시 과금되는 항목만 정리했다.

| 리소스 | 단가 | 시간당 |
|---|---|---|
| Azure Firewall **Basic** | $0.395/시간 + $0.065/GB | ~$0.40 |
| Azure Bastion **Basic** | $0.19/시간 | $0.19 |
| 점프박스 D2s_v5 (Windows) | $0.188/시간 | $0.188 |
| 공인 IP Standard × 3 | ~$0.005/시간 | ~$0.015 |
| **합계** | | **~$0.79/시간** |

- 하루 8시간 실습 ≈ **$6.3**
- 한 달 상시 운영 ≈ **$575**
- 참고: Azure Firewall **Standard**는 $1.25/시간 + 용량 단위 $0.07/시간으로 3배 이상 비싸다.
- Foundry 모델 호출은 토큰 사용량 기반으로 위 표와 별도다.

**실습이 끝나면 반드시 삭제한다.** 방화벽과 Bastion은 유휴 상태에서도 과금된다.

---

## 9. 정리

```bash
azd down --purge
```

또는:

```bash
az group delete -n rg-<env>-private --yes --no-wait
az group delete -n rg-<env>-public  --yes --no-wait
az group delete -n rg-<env>-shared  --yes --no-wait

# Foundry 계정은 soft-delete 되므로 이름 재사용 전에 purge 한다.
az cognitiveservices account list-deleted -o table
az cognitiveservices account purge -n <계정명> -l <리전> -g <리소스그룹>

# 구독 범위에 남는 정책 정의 정리
az policy assignment delete -n assign-subnet-requires-nsg-<env> --scope /subscriptions/<구독ID>/resourceGroups/rg-<env>-private
az policy definition delete -n policy-subnet-requires-nsg-<env>
```

---

## 10. 검증 상태

이 템플릿은 다음까지 확인했다.

- `az bicep build` — 경고·오류 0건
- `az deployment sub validate` (westus3) — 통과
- `az deployment sub what-if` (westus3) — 리소스 46건 Create, 오류 0건
  (역할 할당 2건은 VM 관리 ID가 런타임 값이라 `Unsupported`로 표시되며 정상이다)
- 모델 배포는 실제 프리플라이트로 검증했다. `gpt-4.1-mini`는 `ServiceModelDeprecating`으로 거부되어
  GA 모델 `gpt-5.4-mini`로 교체했고, 교체 후 프리플라이트를 다시 통과했다.
- what-if 페이로드에서 직접 확인한 항목
  - Foundry 양쪽 모두 `disableLocalAuth=true`
  - Private Foundry `publicNetworkAccess=Disabled`, `networkAcls.bypass=None`
  - Public Foundry `defaultAction=Deny` + 노트북 IP + 서브넷 규칙
  - NSG 4종 모두 우선순위 4096 deny-all 보유
  - 방화벽 서브넷 2개를 제외한 모든 서브넷에 NSG 연결
  - UDR `0.0.0.0/0 → VirtualAppliance 10.20.0.4`

**실제 배포(`azd up` / `az deployment sub create`)는 아직 실행하지 않았다.**
방화벽·Bastion 프로비저닝에 20~30분이 걸리고 과금이 시작되므로, 배포 시점은 사용자가 결정한다.
