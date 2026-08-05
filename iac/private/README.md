# 스택 2/3 — Private 망

**리소스 그룹:** `rg-<RGBASENAME>-private` · **다른 스택 의존:** 없음 (언제든 단독 배포/삭제 가능)

VNet 하나에 **Foundry와 VM만** 넣은 구성이다.
Bastion → 점프박스 → Private Endpoint 경로로만 Foundry에 접근한다.

**방화벽은 이 스택에 없다.** 점프박스의 아웃바운드는 NSG(대역·포트)까지만 통제되고 인터넷으로 직접 나간다.
URL(FQDN) 단위로 통제하려면 [스택 3 — private-whitelist](../private-whitelist/)를 쓴다.
그 스택은 자기 VNet·Foundry·VM 한 벌을 따로 갖는 **완전히 독립된 시스템**이라, 이 스택과 나란히 띄워 비교할 수 있다.

| 항목 | 설정 |
|---|---|
| 인증 | keyless — `disableLocalAuth=true`, 실습자 + 점프박스 관리 ID에 RBAC |
| 공용 접근 | `publicNetworkAccess=Disabled` |
| Azure 우회 | `networkAcls.bypass=None` — 신뢰할 수 있는 Azure 서비스도 차단 |
| 접근 경로 | Private Endpoint 단독 (privatelink 존 3개) |
| NSG | 모든 서브넷에 연결, 우선순위 4096 deny-all 기준선 |
| 아웃바운드 | UDR 없음 — 인터넷 직통 (URL 통제 없음) |
| 거버넌스 | "모든 서브넷 NSG 필수" Azure Policy (기본 `Audit`) |

## 주소 배치 (`10.20.0.0/16` 기준)

| 서브넷 | CIDR | NSG | UDR |
|---|---|---|---|
| `AzureBastionSubnet` | `10.20.0.0/26` | 필수 규칙 8개 + deny-all | **걸지 않음**(제어 평면 차단됨) |
| `snet-private-endpoint` | `10.20.1.0/24` | deny-all + 점프박스 443 | — |
| `snet-jumpbox` | `10.20.2.0/24` | deny-all + 최소 허용 | 없음 |

방화벽 서브넷 자리를 비워 두지 않는다 — 이 스택은 방화벽을 쓰지 않으므로 필요가 없다.

## 배포

```bash
az deployment sub create -n hol01-private -l westus3 \
  --template-file main.bicep \
  --parameters resourceGroupBaseName=hol01 location=westus3 \
               labUserPrincipalId="$(az ad signed-in-user show --query id -o tsv)" \
               vmAdminPassword='<12자 이상 복잡한 비밀번호>'
```

또는 이 디렉터리에서 `azd up`.

## 주요 매개변수

| 매개변수 | 기본값 | 설명 |
|---|---|---|
| `vmAdminPassword` | (필수) | 12자 이상, Windows 복잡성 요구사항 충족 |
| `privateVnetAddressPrefix` | `10.20.0.0/16` | public(10.10) · private-whitelist(10.30)와 겹치면 안 된다 |
| `privateEndpointNetworkPolicies` | `Enabled` | PE 트래픽에도 NSG를 적용한다 |
| `subnetNsgPolicyEffect` | `Audit` | 한 번 배포한 뒤 `Deny`로 올리는 것을 권장 |
| `bastionSkuName` | `Basic` | Standard는 네이티브 클라이언트/터널링 지원 |

## 확인

- `SUBNETS_WITHOUT_NSG` 출력이 **비어 있어야** 정상이다. 이 스택에는 NSG를 못 붙이는 플랫폼 서브넷이 없다.
- 노트북에서 `PRIVATE_FOUNDRY_ENDPOINT` 호출은 **실패해야** 정상이다(`publicNetworkAccess=Disabled`).
- 점프박스에서 `nslookup <foundry>.openai.azure.com` 이 `10.20.1.x` 를 반환해야 한다.
- 점프박스에서 아무 사이트나 열린다 — URL 통제가 없는 상태다. 이것이 스택 3과의 차이다.

자세한 배경은 [../README.md](../README.md) 참고.
