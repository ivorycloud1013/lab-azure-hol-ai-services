# 스택 2/3 — Private 망

**리소스 그룹:** `rg-<RGBASENAME>-private` · **다른 스택 의존:** 없음 (언제든 단독으로 배포하고 삭제할 수 있습니다)

하나의 VNet 안에 **Foundry와 VM만** 배포하는 구성입니다.
실습자는 Azure Bastion으로 점프박스 VM에 접속하고, 그 VM에서 Private Endpoint를 통해 Foundry를 호출합니다.

**이 스택에는 방화벽이 없습니다.** 점프박스에서 나가는 트래픽은 NSG가 IP 대역과 포트 수준까지만
확인하고 인터넷으로 바로 나갑니다. 즉 **어떤 사이트든 접속할 수 있습니다.**

접속 가능한 도메인까지 제한하려면 [스택 3 — private-whitelist](../private-whitelist/)를 사용하세요.
그 스택은 이 스택과 별개로 자기 VNet, Foundry, VM을 따로 만들기 때문에,
두 스택을 동시에 배포해 차이를 비교할 수 있습니다.

| 항목 | 설정 |
|---|---|
| 인증 | API 키 없이 Entra ID 토큰만 사용(`disableLocalAuth=true`). 실습자 계정과 점프박스 VM의 관리 ID에 역할 할당 |
| 공용 엔드포인트 | 닫힘 (`publicNetworkAccess=Disabled`) |
| Azure 서비스 예외 통과 | 차단 (`networkAcls.bypass=None`) — 다른 Azure 서비스도 예외로 통과할 수 없습니다 |
| 접근 경로 | Private Endpoint 만 (privatelink DNS 영역 3개 등록) |
| NSG | 모든 서브넷에 연결. 기본은 전부 차단하고 필요한 통신만 허용 |
| 아웃바운드 | Route Table 없음 — 인터넷으로 바로 나감 (도메인 제한 없음) |
| 거버넌스 | "모든 서브넷에 NSG를 연결해야 한다"를 검사하는 Azure Policy (기본값 `Audit`) |

## 서브넷 구성 (`10.20.0.0/16` 기준)

| 서브넷 | 주소 대역 | NSG | Route Table |
|---|---|---|---|
| `AzureBastionSubnet` | `10.20.0.0/26` | Bastion 필수 규칙 8개 + 기본 차단 | **연결하지 않음** (연결하면 Bastion 제어 트래픽이 끊깁니다) |
| `snet-private-endpoint` | `10.20.1.0/24` | 기본 차단 + 점프박스에서 오는 443 허용 | 없음 |
| `snet-jumpbox` | `10.20.2.0/24` | 기본 차단 + 필요한 통신만 허용 | 없음 |

방화벽용 서브넷은 만들지 않습니다. 이 스택은 방화벽을 사용하지 않기 때문입니다.

## 배포

```bash
az deployment sub create -n hol01-private -l westus3 \
  --template-file main.bicep \
  --parameters resourceGroupBaseName=hol01 location=westus3 \
               labUserPrincipalId="$(az ad signed-in-user show --query id -o tsv)" \
               vmAdminPassword='<12자 이상 복잡한 비밀번호>'
```

또는 이 디렉터리에서 `azd up`을 실행합니다.

## 주요 매개변수

| 매개변수 | 기본값 | 설명 |
|---|---|---|
| `vmAdminPassword` | (필수) | 12자 이상이며 Windows 암호 복잡성 요구사항을 만족해야 합니다 |
| `privateVnetAddressPrefix` | `10.20.0.0/16` | public(10.10.0.0/16), private-whitelist(10.30.0.0/16)와 겹치면 안 됩니다 |
| `privateEndpointNetworkPolicies` | `Enabled` | Private Endpoint로 향하는 트래픽에도 NSG 규칙을 적용합니다 |
| `subnetNsgPolicyEffect` | `Audit` | 처음에는 위반 사항을 기록만 합니다. 한 번 배포한 뒤 `Deny`로 올리는 것을 권장합니다 |
| `bastionSkuName` | `Basic` | `Standard`는 네이티브 클라이언트 접속과 터널링을 지원합니다 |

## 배포 후 확인

- `SUBNETS_WITHOUT_NSG` 출력값이 **비어 있어야** 정상입니다.
  이 스택에는 NSG를 연결할 수 없는 서브넷이 없기 때문입니다.
- 노트북에서 `PRIVATE_FOUNDRY_ENDPOINT`를 호출하면 **실패해야** 정상입니다
  (공용 엔드포인트가 닫혀 있습니다).
- 점프박스 VM에서 `nslookup <foundry 계정명>.openai.azure.com` 을 실행하면
  `10.20.1.x` 대역의 사설 IP가 반환되어야 합니다.
- 점프박스에서는 아무 사이트나 접속됩니다. 이것이 스택 3과의 차이입니다.

자세한 배경 설명은 [../README.md](../README.md)를 참고하세요.
