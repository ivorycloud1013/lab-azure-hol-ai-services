# IaC — Azure AI Foundry HOL

**서로 독립된 3개 시스템**으로 구성했다. 각 스택은 자기 리소스 그룹만 만들고, 자기 IaC 루트를 가지며,
따로 배포·갱신·삭제된다. **어떤 스택도 다른 스택을 참조하지 않으므로 배포 순서 제약이 없다.**

| 스택 | 리소스 그룹 | 소유하는 것 | 다른 스택 의존 |
|---|---|---|---|
| [`public/`](public/) | `rg-<RGBASENAME>-public` | Public VNet, Foundry(공용+IP 화이트리스트), 모델, RBAC | **없음** |
| [`private/`](private/) | `rg-<RGBASENAME>-private` | VNet, NSG, Bastion, 점프박스, Foundry(비공개)+PE+DNS | **없음** |
| [`private-whitelist/`](private-whitelist/) | `rg-<RGBASENAME>-private-whitelist` | private과 같은 한 벌 + UDR + Firewall Policy + Azure Firewall | **없음** |

```
┌─ 스택 1 ───────────┐ ┌─ 스택 2 ────────────────┐ ┌─ 스택 3 ────────────────────┐
│ rg-<RGB>-public    │ │ rg-<RGB>-private        │ │ rg-<RGB>-private-whitelist  │
│                    │ │                         │ │                             │
│ vnet 10.10.0.0/16  │ │ vnet 10.20.0.0/16       │ │ vnet 10.30.0.0/16           │
│ Foundry (Enabled)  │ │  ├ AzureBastionSubnet   │ │  ├ AzureFirewallSubnet ──┐  │
│  defaultAction=Deny│ │  ├ snet-private-endpoint│ │  ├ AzureBastionSubnet    │  │
│  ipRules=노트북 IP  │ │  └ snet-jumpbox         │ │  ├ snet-private-endpoint │  │
│                    │ │      └ 인터넷 직통       │ │  └ snet-jumpbox ─UDR→.4 ─┤  │
│ 노트북에서 직접 호출 │ │                         │ │                          ▼  │
│                    │ │ Bastion → 점프박스 → PE  │ │ Azure Firewall              │
│                    │ │ Foundry (Disabled)      │ │  + Firewall Policy (FQDN)   │
│                    │ │                         │ │ Bastion → 점프박스 → PE      │
│                    │ │                         │ │ Foundry (Disabled)          │
│                    │ │                         │ │ Log Analytics               │
└────────────────────┘ └─────────────────────────┘ └─────────────────────────────┘
      독립                   URL 통제 없음               URL 화이트리스트 통제
```

**스택 2와 스택 3의 차이는 딱 세 가지다.**

1. VNet에 `AzureFirewallSubnet`(+Management)을 함께 만든다
2. 점프박스 서브넷에 `0.0.0.0/0 → 방화벽` UDR을 건다
3. Firewall Policy(FQDN 화이트리스트)와 Azure Firewall을 배포한다

나머지(NSG deny-all 기준선, 비공개 Foundry, Bastion, 점프박스, keyless RBAC)는 **같은 공통 모듈**
`modules/workload/private-foundry-workload.bicep` 을 쓴다. 두 스택이 각자 복제하면 보안 기준선이 따로 놀기 때문이다.
두 스택의 차이는 그 모듈의 매개변수 **2개**(`platformSubnets`, `jumpboxRouteTableId`)로만 표현된다.

---

## 배포

```bash
az login
export RGBASENAME=hol01
export REGION=westus3
```

### 스택 1 — Public

```bash
az deployment sub create -n $RGBASENAME-public -l $REGION \
  --template-file iac/public/main.bicep \
  --parameters resourceGroupBaseName=$RGBASENAME location=$REGION \
               labClientIpAddress="$(curl -s ifconfig.me)" \
               labUserPrincipalId="$(az ad signed-in-user show --query id -o tsv)"
```

### 스택 2 — Private (URL 통제 없음)

```bash
az deployment sub create -n $RGBASENAME-private -l $REGION \
  --template-file iac/private/main.bicep \
  --parameters resourceGroupBaseName=$RGBASENAME location=$REGION \
               labUserPrincipalId="$(az ad signed-in-user show --query id -o tsv)" \
               vmAdminPassword='<12자 이상 복잡한 비밀번호>'
```

점프박스는 Bastion으로 접속되고, 아웃바운드는 NSG(대역·포트)까지만 통제된 채 인터넷으로 직접 나간다.

### 스택 3 — Private + URL 화이트리스트

```bash
az deployment sub create -n $RGBASENAME-private-whitelist -l $REGION \
  --template-file iac/private-whitelist/main.bicep \
  --parameters resourceGroupBaseName=$RGBASENAME location=$REGION \
               labUserPrincipalId="$(az ad signed-in-user show --query id -o tsv)" \
               vmAdminPassword='<12자 이상 복잡한 비밀번호>'
```

배포 후 반드시 확인한다.

```bash
az deployment sub show -n $RGBASENAME-private-whitelist \
  --query properties.outputs.FIREWALL_ROUTE_IS_VALID.value
# true 여야 한다. false면 UDR next hop과 실제 방화벽 IP가 어긋난 상태다.
```

### azd로 배포하기

각 스택 디렉터리가 독립된 azd 프로젝트다. 해당 폴더로 이동해서 실행한다.

```bash
cd iac/private && azd env new hol01 && azd env set VM_ADMIN_PASSWORD '<비밀번호>' && azd up
cd ../private-whitelist && azd env new hol01 && azd env set VM_ADMIN_PASSWORD '<비밀번호>' && azd up
```

---

## 화이트리스트만 갱신하기

스택 3을 규칙만 바꿔 다시 민다. 방화벽·정책만 갱신되고 VNet/VM/Foundry는 그대로다.

```bash
az deployment sub create -n $RGBASENAME-private-whitelist -l $REGION \
  --template-file iac/private-whitelist/main.bicep \
  --parameters resourceGroupBaseName=$RGBASENAME location=$REGION \
               vmAdminPassword='<같은 비밀번호>' \
               additionalAllowedFqdns='["github.com","*.githubusercontent.com"]'
```

허용 목록은 `iac/private-whitelist/main.bicep`의 매개변수로 분류돼 있다 —
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
├── governance/ subnet-nsg-policy, policy-assignment
└── workload/   private-foundry-workload   ← private / private-whitelist 공통 한 벌
```

`workload/private-foundry-workload.bicep` 은 리소스 하나가 아니라 **한 벌(VNet + NSG + Bastion + 점프박스 +
비공개 Foundry + PE/DNS + RBAC)** 을 만드는 합성 모듈이다. 스택 2와 3이 이걸 공유한다.

---

## 설계 결정과 제약

### 스택 2와 3을 "기본 + 추가"가 아니라 독립된 두 시스템으로 나눈 이유

이전 구조는 private 스택이 방화벽 서브넷과 UDR을 미리 만들어 두고, 별도 whitelist 스택이 거기에
방화벽을 꽂는 방식이었다. 그러면 private을 단독 배포했을 때 **아웃바운드가 블랙홀**이 되고
(존재하지 않는 방화벽 IP로 강제 터널링), 두 스택의 SKU 설정(`deployFirewallManagementSubnet` ↔
`firewallSkuTier`)도 서로 맞춰야 했다.

지금은 두 스택이 각자 완결된 시스템이다. private은 방화벽을 아예 모르고, private-whitelist는
방화벽까지 자기가 다 만든다. 서로를 참조하지 않으므로 배포 순서도, 맞출 설정도 없다.
대신 **같은 구독에 둘 다 띄워 나란히 비교**하는 실습이 가능해졌다.

### NSG는 VNet과 함께, 예외는 방화벽 서브넷 두 개뿐

NSG는 VNet보다 먼저 만들어져 **서브넷 정의에 인라인으로 연결**된다 — "NSG 없는 서브넷"이 잠깐이라도
존재하는 창이 없다. 강제 수단은 이중이다. `modules/network/vnet.bicep`의 `subnetConfig` 타입이
`networkSecurityGroupId`를 필수 필드로 두어 컴파일 시점에 막고,
`modules/governance/subnet-nsg-policy.bicep`의 Azure Policy가 배포 이후 포털/CLI로 추가되는 서브넷을 막는다.
정책 효과는 기본 `Audit`이다 — `Deny`로 시작하면 첫 배포가 스스로 막힐 수 있어서, 한 번 배포한 뒤 올리는 것을 권장한다.

Azure는 `AzureFirewallSubnet` / `AzureFirewallManagementSubnet`에 **NSG 연결을 지원하지 않는다.**
"모든 서브넷에 NSG" 기준의 유일한 예외이며, `SUBNETS_WITHOUT_NSG` 출력으로 항상 드러나게 했다.
방화벽이 없는 private 스택에서는 이 출력이 **비어 있어야** 정상이다.
`AzureBastionSubnet`은 NSG가 **필수**라 문서화된 필수 규칙 8개를 모두 넣었다.

### NSG는 next hop이 아니라 원래 목적지로 평가된다

UDR로 0.0.0.0/0을 방화벽에 보내더라도 NSG는 패킷의 **원래 목적지**를 본다.
"방화벽 서브넷으로의 아웃바운드 허용"만으로는 인터넷 트래픽이 통과하지 못한다.
점프박스 NSG가 `Internet:80/443`을 L4에서 넓게 허용하고, 실제 FQDN 통제는 방화벽이 맡는 이유다.
**NSG는 대역·포트, 방화벽은 도메인** — 역할이 다르다.

### UDR next hop을 계산으로 구한 이유

`VNet → Firewall → RouteTable → VNet` 순환 의존을 피하기 위해 `cidrHost(firewallSubnetPrefix, 3)`로 계산한다.
Azure Firewall은 전용 서브넷에서 항상 첫 할당 가능 주소(`x.x.x.4`)를 받는다
(`.0` 네트워크 / `.1` 게이트웨이 / `.2` `.3` 예약).
덕분에 한 스택 안에서 Route Table → VNet → Firewall 순으로 순환 없이 배포된다.
계산값과 실제값의 일치는 `FIREWALL_ROUTE_IS_VALID` 출력으로 검증한다.

### AzureBastionSubnet에는 UDR을 걸지 않았다

`0.0.0.0/0`을 방화벽으로 보내면 Bastion 제어 평면이 끊겨 세션이 열리지 않는다. 의도적으로 제외했다.

### DNS는 Azure 제공 DNS를 사용한다

Azure Firewall DNS 프록시를 켜면 VNet DNS를 방화벽 IP로 바꿔야 한다.
Private Endpoint 이름 해석은 VNet에 링크된 Private DNS Zone만으로 충분하고,
FQDN 필터링도 **애플리케이션 규칙**에서는 DNS 프록시 없이 동작하므로 켜지 않았다.
네트워크 규칙에 FQDN을 쓸 때만 필요하다.

### Private Endpoint 서브넷의 `privateEndpointNetworkPolicies=Enabled`

기본값은 `Disabled`(= PE 트래픽에 NSG 미적용)다. deny-all 기준을 PE에도 실제로 적용하기 위해
`Enabled`로 두고 점프박스 서브넷에서의 443만 명시 허용했다.

### Azure Firewall Basic의 추가 요구사항

Basic SKU는 `AzureFirewallManagementSubnet`과 **별도 공인 IP**를 요구한다.
둘 다 private-whitelist 스택이 `firewallSkuTier` 값에 따라 알아서 만든다 — 다른 스택과 맞출 설정이 없다.
Standard/Premium으로 바꾸면 관리 서브넷 없이 배포된다.

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
private-whitelist만 기본 `true`다 — 방화벽 화이트리스트 실습은 무엇이 차단됐는지 못 보면 성립하지 않기 때문이다.
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
az network bastion rdp --name <BASTION_NAME> --resource-group rg-$RGBASENAME-private \
  --target-resource-id $(az vm show -g rg-$RGBASENAME-private -n <JUMPBOX_NAME> --query id -o tsv)
```

```powershell
nslookup <private-foundry>.openai.azure.com   # 10.20.1.x 를 반환해야 정상
```

`DefaultAzureCredential()`이 VM 관리 ID를 자동으로 집어 키 없이 호출된다.

### 4. URL 통제 비교 — 스택 2 vs 스택 3

같은 명령을 두 점프박스에서 각각 실행한다. **차이가 곧 화이트리스트의 효과다.**

```powershell
Invoke-WebRequest https://login.microsoftonline.com
Invoke-WebRequest https://github.com
```

| | 스택 2 (private) | 스택 3 (private-whitelist) |
|---|---|---|
| `login.microsoftonline.com` | 성공 | 성공 (화이트리스트) |
| `github.com` | **성공** — 통제 없음 | **차단** — 목록에 없음 |

스택 3의 차단 로그:

```kusto
AZFWApplicationRule
| where TimeGenerated > ago(30m)
| project TimeGenerated, SourceIp, Fqdn, Action, Rule
| order by TimeGenerated desc
```

`additionalAllowedFqdns`에 `github.com`을 넣고 스택 3만 다시 밀면 차단이 풀린다.

### 5. NSG 거버넌스 확인

```bash
az policy state list --resource-group rg-$RGBASENAME-private \
  --filter "policyDefinitionName eq 'policy-subnet-requires-nsg-$RGBASENAME-private'" \
  --query "[].{res:resourceId, state:complianceState}" -o table
```

---

## 비용

westus3 소매가 기준(USD, 2026-08 Azure Retail Prices API 조회값). 상시 과금 항목만 정리했다.

| 스택 | 리소스 | 시간당 |
|---|---|---|
| private-whitelist | Firewall Basic($0.395/h + $0.065/GB) + 공인 IP 2개 + Bastion Basic($0.19/h) + 점프박스 D2s_v5 Windows($0.188/h) | **~$0.79** |
| private | Bastion Basic($0.19/h) + 점프박스 D2s_v5 Windows($0.188/h) + 공인 IP | **~$0.38** |
| public | VNet/NSG/Foundry — 유휴 시 과금 없음 | **~$0** |
| | **셋 다 켜 둘 때 합계** | **~$1.17/시간** |

- 비교 실습이 끝나면 **스택 3만 지워도 시간당 $0.79가 즉시 절약된다.**
- Azure Firewall **Standard**는 $1.25/h + 용량 단위 $0.07/h로 3배 이상 비싸다.
- Foundry 모델 호출은 토큰 사용량 기반으로 위 표와 별도다.

---

## 정리

스택 간 의존이 없으므로 순서는 상관없다.

```bash
az group delete -n rg-$RGBASENAME-private-whitelist --yes --no-wait
az group delete -n rg-$RGBASENAME-private           --yes --no-wait
az group delete -n rg-$RGBASENAME-public            --yes --no-wait

# Foundry 계정은 soft-delete 되므로 이름 재사용 전에 purge 한다.
az cognitiveservices account list-deleted -o table
az cognitiveservices account purge -n <계정명> -l <리전> -g <리소스그룹>

# 두 스택이 구독 범위에 만든 정책 정의 정리
for STACK in private private-whitelist; do
  az policy assignment delete -n assign-subnet-requires-nsg-$RGBASENAME-$STACK \
    --scope /subscriptions/<구독ID>/resourceGroups/rg-$RGBASENAME-$STACK
  az policy definition delete -n policy-subnet-requires-nsg-$RGBASENAME-$STACK
done
```

---

## 검증 상태

westus3 · `resourceGroupBaseName=holv3` 로 실행한 결과다.

| 항목 | public | private | private-whitelist |
|---|---|---|---|
| `az bicep build` (경고·오류 0) | 통과 | 통과 | 통과 |
| `az deployment sub validate` | 통과 | 통과 | 통과 |
| `az deployment sub what-if` | Create 6건 | Create 22건 | Create 37건 |

what-if 페이로드에서 직접 확인한 것:

- 각 스택이 **자기 리소스 그룹 하나만** 건드린다 (`rg-holv3-public` / `rg-holv3-private` / `rg-holv3-private-whitelist`)
- private 스택에는 방화벽·Route Table 리소스가 **0건**이다. 서브넷도 3개(`AzureBastionSubnet`,
  `snet-private-endpoint`, `snet-jumpbox`)뿐이고 **전부 NSG가 붙어 있으며 UDR이 없다**
- private-whitelist 스택은 `AzureFirewallSubnet`(10.30.0.0/26) + `AzureFirewallManagementSubnet`(10.30.0.64/26)을
  자기 VNet에 만들고, `snet-jumpbox`에 `default-to-firewall 0.0.0.0/0 → VirtualAppliance 10.30.0.4` UDR을 건다
- 두 private 계열 스택의 NSG·Foundry 이름이 스택별로 갈린다 (`nsg-holv3-private-*` ↔ `nsg-holv3-private-whitelist-*`)
  — 같은 구독에 동시 배포 가능하다

**한계 — 아직 검증하지 못한 것:**

- **실제 배포는 하지 않았다.** 방화벽·Bastion 프로비저닝에 20~30분이 걸리고 과금이 시작된다.
- 방화벽 실제 사설 IP가 `cidrHost` 계산값(`10.30.0.4`)과 일치하는지는 실배포 후
  `FIREWALL_ROUTE_IS_VALID` 로 확인해야 한다.
