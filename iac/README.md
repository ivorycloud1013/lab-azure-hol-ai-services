# IaC — Azure AI Foundry HOL

**서로 독립된 3개 시스템**으로 구성했다. 각 스택은 자기 리소스 그룹만 만들고, 자기 IaC 루트를 가지며,
따로 배포·갱신·삭제된다. 하나를 지워도 나머지는 살아 있다.

| 스택 | 리소스 그룹 | 소유하는 것 | 다른 스택 의존 |
|---|---|---|---|
| [`public/`](public/) | `rg-<env>-public` | Public VNet, Foundry(공용+IP 화이트리스트), 모델, RBAC | **없음** |
| [`private/`](private/) | `rg-<env>-private` | Private VNet, NSG, UDR, Bastion, 점프박스, Foundry(비공개)+PE+DNS, NSG 정책 | **없음** |
| [`whitelist/`](whitelist/) | `rg-<env>-whitelist` | Firewall Policy(FQDN 화이트리스트), Azure Firewall | `private` 먼저 |

```
┌─ 스택 1 ─────────────┐   ┌─ 스택 2 ────────────────────┐   ┌─ 스택 3 ─────────┐
│ rg-<env>-public      │   │ rg-<env>-private            │   │ rg-<env>-whitelist│
│                      │   │                             │   │                   │
│ vnet 10.10.0.0/16    │   │ vnet 10.20.0.0/16           │◀──│ Azure Firewall    │
│ Foundry (Enabled)    │   │  ├ AzureFirewallSubnet ─────┼───│  (서브넷만 빌려씀) │
│  defaultAction=Deny  │   │  ├ AzureBastionSubnet       │   │ Firewall Policy   │
│  ipRules=노트북 IP    │   │  ├ snet-private-endpoint    │   │  FQDN 화이트리스트 │
│                      │   │  └ snet-jumpbox ─UDR→.4 ────┼──▶│                   │
│ 노트북에서 직접 호출   │   │ Bastion → 점프박스 → PE      │   │ Log Analytics     │
└──────────────────────┘   │ Foundry (Disabled, bypass=None)│└───────────────────┘
                           └─────────────────────────────┘
```

**스택 간 결합은 단 하나** — Azure Firewall은 반드시 보호 대상 VNet의 `AzureFirewallSubnet`에 있어야 한다.
그래서 whitelist 스택은 private 스택이 만들어 둔 서브넷을 이름으로 참조한다(방화벽 리소스 자체는 자기 RG에 생성됨).
private 스택의 UDR next hop은 방화벽 IP를 `cidrHost()`로 **미리 계산**해 두므로, private은 방화벽 없이도 단독 배포된다.

---

## 배포

```bash
az login
export ENV=hol01
export LOC=westus3
```

### 스택 1 — Public (독립, 언제든)

```bash
az deployment sub create -n $ENV-public -l $LOC \
  --template-file iac/public/main.bicep \
  --parameters resourceGroupBaseName=$ENV location=$LOC \
               labClientIpAddress="$(curl -s ifconfig.me)" \
               labUserPrincipalId="$(az ad signed-in-user show --query id -o tsv)"
```

### 스택 2 — Private

```bash
az deployment sub create -n $ENV-private -l $LOC \
  --template-file iac/private/main.bicep \
  --parameters resourceGroupBaseName=$ENV location=$LOC \
               labUserPrincipalId="$(az ad signed-in-user show --query id -o tsv)" \
               vmAdminPassword='<12자 이상 복잡한 비밀번호>'
```

이 시점에서 점프박스는 **아웃바운드가 블랙홀**이다. UDR이 아직 존재하지 않는 방화벽을 가리키기 때문이다.
Bastion 서브넷에는 UDR을 걸지 않으므로 **접속은 된다.** 실습에서 "차단된 상태"를 먼저 보여주기 좋은 지점이다.

### 스택 3 — Whitelist

private 스택의 출력을 그대로 입력으로 넘긴다.

```bash
PRV=$(az deployment sub show -n $ENV-private --query properties.outputs -o json)

az deployment sub create -n $ENV-whitelist -l $LOC \
  --template-file iac/whitelist/main.bicep \
  --parameters resourceGroupBaseName=$ENV location=$LOC \
               privateVnetResourceGroupName=$(echo $PRV | jq -r .PRIVATE_RESOURCE_GROUP.value) \
               privateVnetName=$(echo $PRV | jq -r .PRIVATE_VNET_NAME.value) \
               expectedFirewallPrivateIp=$(echo $PRV | jq -r .EXPECTED_FIREWALL_PRIVATE_IP.value)
```

배포 후 반드시 확인한다.

```bash
az deployment sub show -n $ENV-whitelist --query properties.outputs.FIREWALL_ROUTE_IS_VALID.value
# true 여야 한다. false면 UDR next hop과 실제 방화벽 IP가 어긋난 상태다.
```

### azd로 배포하기

각 스택 디렉터리가 독립된 azd 프로젝트다. 해당 폴더로 이동해서 실행한다.

```bash
cd iac/private && azd env new hol01 && azd env set VM_ADMIN_PASSWORD '<비밀번호>' && azd up
cd ../whitelist && azd env new hol01 && azd env set PRIVATE_RESOURCE_GROUP rg-hol01-private \
  && azd env set PRIVATE_VNET_NAME vnet-hol01-private && azd up
```

---

## 화이트리스트만 갱신하기

이 구조의 핵심 이점이다. private 망을 전혀 건드리지 않고 규칙만 다시 민다.

```bash
az deployment sub create -n $ENV-whitelist -l $LOC \
  --template-file iac/whitelist/main.bicep \
  --parameters resourceGroupBaseName=$ENV location=$LOC \
               privateVnetResourceGroupName=rg-$ENV-private \
               privateVnetName=vnet-$ENV-private \
               additionalAllowedFqdns='["github.com","*.githubusercontent.com"]'
```

허용 목록은 `iac/whitelist/main.bicep`의 매개변수로 분류돼 있다 —
`identityAndManagementFqdns` / `foundryFqdns` / `portalFqdns` / `toolingFqdns` / `additionalAllowedFqdns` /
`allowedServiceTags` / `allowedFqdnTags`.

---

## 공유 모듈

`iac/modules/` 는 세 스택이 함께 쓰는 빌딩 블록이다. **시스템은 분리하되 블록까지 복제하지는 않았다** —
NSG deny-all 기준선이나 Foundry keyless 설정 같은 규칙이 스택마다 따로 놀면 드리프트가 생기기 때문이다.

```
modules/
├── network/    nsg, vnet, route-table, firewall-policy, firewall, bastion,
│               private-dns-zone, private-endpoint
├── ai/         foundry-account, foundry-project, model-deployments
├── compute/    jumpbox
├── identity/   role-definitions, foundry-role-assignments
├── monitor/    log-analytics
└── governance/ subnet-nsg-policy, policy-assignment
```

---

## 설계 결정과 제약

### NSG 예외는 방화벽 서브넷 두 개뿐

Azure는 `AzureFirewallSubnet` / `AzureFirewallManagementSubnet`에 **NSG 연결을 지원하지 않는다.**
"모든 서브넷에 NSG" 기준의 유일한 예외이며, `SUBNETS_WITHOUT_NSG` 출력으로 항상 드러나게 했다.
`AzureBastionSubnet`은 NSG가 **필수**라 문서화된 필수 규칙 8개를 모두 넣었다.

강제 수단은 이중이다. `modules/network/vnet.bicep`의 `subnetConfig` 타입이 `networkSecurityGroupId`를
필수 필드로 두어 컴파일 시점에 막고, `modules/governance/subnet-nsg-policy.bicep`의 Azure Policy가
배포 이후 포털/CLI로 추가되는 서브넷을 막는다. 정책 효과는 기본 `Audit`이다 —
`Deny`로 시작하면 첫 배포가 스스로 막힐 수 있어서, 한 번 배포한 뒤 올리는 것을 권장한다.

### NSG는 next hop이 아니라 원래 목적지로 평가된다

UDR로 0.0.0.0/0을 방화벽에 보내더라도 NSG는 패킷의 **원래 목적지**를 본다.
"방화벽 서브넷으로의 아웃바운드 허용"만으로는 인터넷 트래픽이 통과하지 못한다.
점프박스 NSG가 `Internet:80/443`을 L4에서 넓게 허용하고, 실제 FQDN 통제는 whitelist 스택의 방화벽이 맡는 이유다.
**NSG는 대역·포트, 방화벽은 도메인** — 역할이 다르다.

### UDR next hop을 계산으로 구한 이유

`VNet → Firewall → RouteTable → VNet` 순환 의존을 피하기 위해 `cidrHost(firewallSubnetPrefix, 3)`로 계산한다.
Azure Firewall은 전용 서브넷에서 항상 첫 할당 가능 주소(`x.x.x.4`)를 받는다
(`.0` 네트워크 / `.1` 게이트웨이 / `.2` `.3` 예약).
이 계산 덕분에 **private 스택이 whitelist 스택 없이 단독 배포된다** — 스택 분리를 가능하게 한 핵심이다.
계산값과 실제값의 일치는 whitelist 스택의 `FIREWALL_ROUTE_IS_VALID` 출력으로 검증한다.

### AzureBastionSubnet에는 UDR을 걸지 않았다

`0.0.0.0/0`을 방화벽으로 보내면 Bastion 제어 평면이 끊겨 세션이 열리지 않는다. 의도적으로 제외했다.
덕분에 whitelist 스택 배포 전에도 점프박스 접속은 가능하다.

### DNS는 Azure 제공 DNS를 사용한다

Azure Firewall DNS 프록시를 켜면 VNet DNS를 방화벽 IP로 바꿔야 해서 private 스택이 whitelist 스택에
의존하게 된다. Private Endpoint 이름 해석은 VNet에 링크된 Private DNS Zone만으로 충분하고,
FQDN 필터링도 **애플리케이션 규칙**에서는 DNS 프록시 없이 동작하므로 켜지 않았다.
네트워크 규칙에 FQDN을 쓸 때만 필요하다.

### Private Endpoint 서브넷의 `privateEndpointNetworkPolicies=Enabled`

기본값은 `Disabled`(= PE 트래픽에 NSG 미적용)다. deny-all 기준을 PE에도 실제로 적용하기 위해
`Enabled`로 두고 점프박스 서브넷에서의 443만 명시 허용했다.

### Azure Firewall Basic의 추가 요구사항

Basic SKU는 `AzureFirewallManagementSubnet`과 **별도 공인 IP**를 요구한다.
서브넷은 private 스택이, 공인 IP는 whitelist 스택이 만든다.
**두 스택의 설정이 맞아야 한다** — private의 `deployFirewallManagementSubnet`과
whitelist의 `firewallSkuTier`. Standard/Premium을 쓰려면 private을 `false`로 재배포한다.

### 모델 선택 — `gpt-4.x` 계열은 쓸 수 없다

`az cognitiveservices model list`에 나온다고 배포 가능한 것이 아니다. 목록에는 **Deprecating 상태 모델도
함께 나오는데**, 이들은 제어 평면 프리플라이트에서 거부된다.

```
ServiceModelDeprecating - The model 'Format:OpenAI,Name:gpt-4.1-mini,Version:2025-04-14'
is in deprecating state and cannot be used for new deployments.
```

`gpt-4o`, `gpt-4o-mini`, `gpt-4.1`, `gpt-4.1-mini`, `gpt-4.1-nano` 전부 해당한다.
기본값을 `gpt-5.4-mini`(2026-03-17, GenerallyAvailable)로 잡은 이유다. 모델을 바꿀 때는 반드시 확인한다.

```bash
az cognitiveservices model list -l westus3 \
  --query "[?model.name=='<모델명>'].{v:model.version, ls:model.lifecycleStatus, s:join(',',model.skus[].name)}" -o table
```

### Log Analytics는 스택마다 별도

스택 독립성을 지키기 위해 각 스택이 자기 작업 영역을 선택적으로 만든다.
whitelist만 기본 `true`다 — 방화벽 화이트리스트 실습은 무엇이 차단됐는지 못 보면 성립하지 않기 때문이다.
하나로 합치고 싶으면 `existingLogAnalyticsWorkspaceId`에 기존 작업 영역 ID를 넘기면 된다.
일일 수집 상한은 1GB로 묶어 두었다.

### `bicepconfig.json`에서 `no-hardcoded-env-urls`를 끈 이유

이 규칙은 하드코딩된 Azure URL을 경고하지만, FQDN 화이트리스트는 `login.microsoftonline.com` 같은 도메인을
**문자열 그대로 적는 것이 목적**이다. 대신 `no-unused-params` 등 실질적인 규칙은 `error`로 올렸다.

---

## 실습 시나리오

### 1. Public 망 — keyless 호출 (노트북)

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

키가 아예 없다. `disableLocalAuth=true` 라서 `az cognitiveservices account keys list` 도 실패한다.
노트북을 다른 네트워크로 옮기면 IP 화이트리스트를 벗어나 `403`이 난다.

### 2. Private 망 — 노트북에서는 실패해야 정상

같은 코드로 `PRIVATE_FOUNDRY_ENDPOINT`를 호출하면 실패한다. `publicNetworkAccess=Disabled`라 공용 경로가 없다.

### 3. Private 망 — Bastion 경유 성공

```bash
az network bastion rdp --name <BASTION_NAME> --resource-group rg-<env>-private \
  --target-resource-id $(az vm show -g rg-<env>-private -n <JUMPBOX_NAME> --query id -o tsv)
```

```powershell
nslookup <private-foundry>.openai.azure.com   # 10.20.1.x 를 반환해야 정상
```

`DefaultAzureCredential()`이 VM 관리 ID를 자동으로 집어 키 없이 호출된다.

### 4. 화이트리스트 체감 — 스택 3의 있고 없음

```powershell
Invoke-WebRequest https://login.microsoftonline.com  # 허용 (화이트리스트)
Invoke-WebRequest https://github.com                 # 차단 (목록에 없음)
```

```kusto
AZFWApplicationRule
| where TimeGenerated > ago(30m)
| project TimeGenerated, SourceIp, Fqdn, Action, Rule
| order by TimeGenerated desc
```

whitelist 스택을 지웠다가(`az group delete -n rg-<env>-whitelist`) 다시 배포하면
**같은 private 망이 완전 차단 ↔ 화이트리스트 통과 사이를 오간다.** 스택을 나눈 덕에 가능한 시연이다.

### 5. NSG 거버넌스 확인

```bash
az policy state list --resource-group rg-<env>-private \
  --filter "policyDefinitionName eq 'policy-subnet-requires-nsg-<env>'" \
  --query "[].{res:resourceId, state:complianceState}" -o table
```

---

## 비용

westus3 소매가 기준(USD, 2026-08 Azure Retail Prices API 조회값). 상시 과금 항목만 정리했다.

| 스택 | 리소스 | 시간당 |
|---|---|---|
| whitelist | Azure Firewall Basic ($0.395/h + $0.065/GB) + 공인 IP 2개 | **~$0.41** |
| private | Bastion Basic ($0.19/h) + 점프박스 D2s_v5 Windows ($0.188/h) + 공인 IP | **~$0.38** |
| public | VNet/NSG/Foundry — 유휴 시 과금 없음 | **~$0** |
| | **합계** | **~$0.79/시간** |

- 하루 8시간 실습 ≈ **$6.3** / 한 달 상시 ≈ **$575**
- Azure Firewall **Standard**는 $1.25/h + 용량 단위 $0.07/h로 3배 이상 비싸다.
- Foundry 모델 호출은 토큰 사용량 기반으로 위 표와 별도다.

**스택이 나뉘어 있어 비용도 따로 끌 수 있다.** 실습을 쉬는 동안 whitelist만 지우면 시간당 $0.41이 즉시 절약된다.

---

## 정리

역순으로 지운다.

```bash
az group delete -n rg-$ENV-whitelist --yes --no-wait
az group delete -n rg-$ENV-private   --yes --no-wait
az group delete -n rg-$ENV-public    --yes --no-wait

# Foundry 계정은 soft-delete 되므로 이름 재사용 전에 purge 한다.
az cognitiveservices account list-deleted -o table
az cognitiveservices account purge -n <계정명> -l <리전> -g <리소스그룹>

# private 스택이 구독 범위에 만든 정책 정의 정리
az policy assignment delete -n assign-subnet-requires-nsg-$ENV \
  --scope /subscriptions/<구독ID>/resourceGroups/rg-$ENV-private
az policy definition delete -n policy-subnet-requires-nsg-$ENV
```

---

## 검증 상태

| 항목 | public | private | whitelist |
|---|---|---|---|
| `az bicep build` (경고·오류 0) | 통과 | 통과 | 통과 |
| `az deployment sub validate` (westus3) | 통과 | 통과 | 통과 |
| `az deployment sub what-if` (westus3) | Create 6건 | Create 23건 | — |

what-if 페이로드에서 직접 확인한 것:

- 각 스택이 **자기 리소스 그룹 하나만** 만든다 (`rg-holv2-public` / `rg-holv2-private`)
- Azure Firewall / Firewall Policy가 public·private 스택 어디에도 **섞여 들어가지 않았다**
- private VNet이 `AzureFirewallSubnet` / `AzureFirewallManagementSubnet` 자리를 예약해 둔다
- UDR `0.0.0.0/0 → 10.20.0.4`
- 이전 통합 버전에서 확인한 보안 속성(Foundry 양쪽 `disableLocalAuth=true`,
  private `publicNetworkAccess=Disabled`+`bypass=None`, public `defaultAction=Deny`+IP 규칙,
  NSG 4종 모두 4096 deny-all)은 동일한 모듈을 그대로 쓰므로 유지된다

**한계 — 아직 검증하지 못한 것:**

- **실제 배포는 하지 않았다.** 방화벽·Bastion 프로비저닝에 20~30분이 걸리고 과금이 시작된다.
- whitelist 스택은 `validate`만 통과했고 `what-if`는 돌리지 않았다.
  ARM은 이 단계에서 **다른 RG의 서브넷 존재 여부를 확인하지 않으므로**,
  "private 없이도 whitelist가 배포된다"는 뜻이 **아니다.**
  실제 배포 시 `AzureFirewallSubnet`이 없으면 실패한다. **private → whitelist 순서는 반드시 지켜야 한다.**
