# 스택 2/3 — Private 망

**리소스 그룹:** `rg-<env>-private` · **다른 스택 의존:** 없음 (단, whitelist보다 **먼저** 배포해야 한다)

Bastion → 점프박스 → Private Endpoint 경로로만 Foundry에 접근하는 구성이다.
방화벽은 이 스택에 **없다** — [스택 3](../whitelist/)이 소유한다.

| 항목 | 설정 |
|---|---|
| 인증 | keyless — `disableLocalAuth=true`, 실습자 + 점프박스 관리 ID에 RBAC |
| 공용 접근 | `publicNetworkAccess=Disabled` |
| Azure 우회 | `networkAcls.bypass=None` — 신뢰할 수 있는 Azure 서비스도 차단 |
| 접근 경로 | Private Endpoint 단독 (privatelink 존 3개) |
| NSG | 모든 서브넷에 연결, 우선순위 4096 deny-all 기준선 |
| 거버넌스 | "모든 서브넷 NSG 필수" Azure Policy (기본 `Audit`) |

## 주소 배치 (`10.20.0.0/16` 기준)

| 서브넷 | CIDR | NSG | UDR |
|---|---|---|---|
| `AzureFirewallSubnet` | `10.20.0.0/26` | 불가(플랫폼 제약) | — |
| `AzureFirewallManagementSubnet` | `10.20.0.64/26` | 불가(플랫폼 제약) | — |
| `AzureBastionSubnet` | `10.20.0.128/26` | 필수 규칙 8개 + deny-all | **걸지 않음**(제어 평면 차단됨) |
| `snet-private-endpoint` | `10.20.1.0/24` | deny-all + 점프박스 443 | — |
| `snet-jumpbox` | `10.20.2.0/24` | deny-all + 최소 허용 | `0.0.0.0/0 → 10.20.0.4` |

## 배포

```bash
az deployment sub create -n hol01-private -l westus3 \
  --template-file main.bicep \
  --parameters environmentName=hol01 location=westus3 \
               labUserPrincipalId="$(az ad signed-in-user show --query id -o tsv)" \
               vmAdminPassword='<12자 이상 복잡한 비밀번호>'
```

**배포 직후 점프박스는 아웃바운드가 블랙홀이다.** UDR이 아직 없는 방화벽(`10.20.0.4`)을 가리키기 때문이다.
Bastion 접속은 정상이다. whitelist 스택을 배포하면 화이트리스트대로 뚫린다.

## 주요 매개변수

| 매개변수 | 기본값 | 설명 |
|---|---|---|
| `vmAdminPassword` | (필수) | 12자 이상, Windows 복잡성 요구사항 충족 |
| `deployFirewallManagementSubnet` | `true` | whitelist가 Firewall **Basic**이면 필수. Standard면 `false` |
| `privateEndpointNetworkPolicies` | `Enabled` | PE 트래픽에도 NSG를 적용한다 |
| `subnetNsgPolicyEffect` | `Audit` | 한 번 배포한 뒤 `Deny`로 올리는 것을 권장 |
| `bastionSkuName` | `Basic` | Standard는 네이티브 클라이언트/터널링 지원 |

## 확인

- `SUBNETS_WITHOUT_NSG` 출력은 `["AzureFirewallSubnet","AzureFirewallManagementSubnet"]` **만** 나와야 한다.
- 아래 5개 출력을 whitelist 스택 입력으로 넘긴다:
  `PRIVATE_RESOURCE_GROUP`, `PRIVATE_VNET_NAME`, `PROTECTED_SOURCE_ADDRESSES`,
  `EXPECTED_FIREWALL_PRIVATE_IP`, `FIREWALL_MANAGEMENT_SUBNET_DEPLOYED`

자세한 배경은 [../README.md](../README.md) 참고.
