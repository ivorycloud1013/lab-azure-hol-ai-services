# 시스템 2/3 — Private 망

**리소스 그룹:** `rg-<RGBASENAME>-private` · **다른 시스템 의존:** 없음 (언제든 단독으로 배포하고 삭제할 수 있습니다)

하나의 VNet 안에 **Foundry와 Azure Machine Learning, 그리고 VM을** 배포하는 구성입니다.
실습자는 Azure Bastion으로 점프박스 VM에 접속하고, 그 VM에서 Private Endpoint를 통해 Foundry와 AML을 호출합니다.

**이 시스템에는 방화벽이 없습니다.** 점프박스에서 나가는 트래픽은 NSG가 IP 대역과 포트 수준까지만
확인하고 인터넷으로 바로 나갑니다. 즉 **어떤 사이트든 접속할 수 있습니다.**

접속 가능한 도메인까지 제한하려면 [시스템 3 — private-whitelist](../private-whitelist/)를 사용하세요.
그 시스템은 이 시스템과 별개로 자기 VNet, Foundry, VM을 따로 만들기 때문에,
두 시스템을 동시에 배포해 차이를 비교할 수 있습니다.

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

| 서브넷 | 주소 대역 | 용도 | NSG | Route Table |
|---|---|---|---|---|
| `AzureBastionSubnet` | `10.20.0.0/26` | Azure Bastion | Bastion 필수 규칙 8개 + 기본 차단 | **연결하지 않음** (연결하면 Bastion 제어 트래픽이 끊깁니다) |
| `snet-private-endpoint` | `10.20.1.0/24` | Foundry Private Endpoint | 기본 차단 + 점프박스에서 오는 443 허용 | 없음 |
| `snet-jumpbox` | `10.20.2.0/24` | 점프박스 VM | 기본 차단 + 필요한 통신만 허용 | 없음 |
| `snet-aml` | `10.20.3.0/24` | AML 워크스페이스 · 스토리지 · Key Vault의 Private Endpoint | 기본 차단 + 점프박스에서 오는 443 허용 | 없음 |

`snet-aml`은 `deployMachineLearning=true`(기본값)일 때만 만들어집니다.
Foundry용 서브넷과 분리해 둔 이유는, 두 서비스의 접근 경로를 NSG로 따로 통제하고
어떤 트래픽이 어디로 가는지 서브넷 단위로 드러나게 하기 위해서입니다.

방화벽용 서브넷은 만들지 않습니다. 이 시스템은 방화벽을 사용하지 않기 때문입니다.

## Azure Machine Learning

| 리소스 | 잠그는 방식 |
|---|---|
| 워크스페이스 | `publicNetworkAccess=Disabled` — 공용 엔드포인트 자체가 없습니다. `amlworkspace` Private Endpoint만이 유일한 경로입니다 |
| 스토리지 계정 (기본 데이터스토어) | `networkAcls.defaultAction=Deny` + `bypass=AzureServices`, `blob`/`file` Private Endpoint |
| Key Vault (비밀 저장소) | `networkAcls.defaultAction=Deny` + `bypass=AzureServices`, `vault` Private Endpoint, RBAC 인증 |
| Application Insights (+ Log Analytics) | 워크스페이스의 **필수** 의존 리소스입니다. 셋(스토리지 · Key Vault · App Insights) 중 하나라도 없으면 `Missing dependent resources in workspace json` 으로 배포가 실패합니다. 클래식 App Insights는 사용이 중단되어 Log Analytics 작업 영역에 연결된 형태로 만듭니다 |
| 권한 | 실습자 계정과 점프박스 관리 ID에 **AzureML Data Scientist** 역할 |

**스토리지와 Key Vault만 `publicNetworkAccess=Disabled`가 아닌 이유**
Azure Machine Learning 리소스 공급자가 워크스페이스를 만들 때 "신뢰할 수 있는 Azure 서비스" 자격으로
두 리소스에 접근해야 합니다. `Disabled`로 내리면 공용 엔드포인트 자체가 사라져 `networkAcls.bypass`가
평가되지 않고, **워크스페이스 생성이 실패합니다.** 그래서 Microsoft 공식 보안 레퍼런스 템플릿과 같이
공용 엔드포인트는 남기되 허용 목록을 비운 `Deny` 상태로 둡니다. 허용된 IP도 VNet도 없으므로
인터넷의 어떤 클라이언트도 도달할 수 없고, 실제 데이터 경로는 Private Endpoint뿐입니다.

**컴퓨팅(Compute Instance / Cluster)은 이 템플릿이 만들지 않습니다.**
워크스페이스의 **관리형 네트워크**(`machineLearningIsolationMode`, 기본값 `AllowInternetOutbound`)를
켜 두었으므로, 실습 중에 컴퓨팅을 만들면 이 VNet이 아니라 **Azure가 관리하는 별도 VNet** 안에 생성되고
스토리지·Key Vault에는 관리형 Private Endpoint로 접근합니다. 컴퓨팅용 서브넷을 VNet에 직접 주입하는
예전 방식보다 이 방식이 권장됩니다. 관리형 네트워크는 첫 컴퓨팅을 만들 때 프로비저닝되므로
이 템플릿의 배포 시간에는 영향을 주지 않습니다.

> **재배포 시 주의** — Key Vault는 소프트 삭제가 강제됩니다. `make destroy-private` 후 같은
> `RGBASENAME`·리전으로 다시 배포하면 이름 충돌이 납니다. 이때는 `az keyvault purge --name <이름> --location <리전>`
> 으로 완전히 지운 뒤 다시 배포하세요. purge protection은 켜지 않았으므로 purge가 가능합니다.

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
| `deployMachineLearning` | `true` | `false`로 두면 `snet-aml`과 AML 관련 리소스를 만들지 않습니다 |
| `machineLearningIsolationMode` | `AllowInternetOutbound` | 컴퓨팅이 놓일 관리형 네트워크의 아웃바운드 정책입니다. `AllowOnlyApprovedOutbound`가 가장 엄격합니다 |
| `privateEndpointNetworkPolicies` | `Enabled` | Private Endpoint로 향하는 트래픽에도 NSG 규칙을 적용합니다 |
| `subnetNsgPolicyEffect` | `Audit` | 처음에는 위반 사항을 기록만 합니다. 한 번 배포한 뒤 `Deny`로 올리는 것을 권장합니다 |
| `bastionSkuName` | `Basic` | `Standard`는 네이티브 클라이언트 접속과 터널링을 지원합니다 |

## 배포 후 확인

- `SUBNETS_WITHOUT_NSG` 출력값이 **비어 있어야** 정상입니다.
  이 시스템에는 NSG를 연결할 수 없는 서브넷이 없기 때문입니다.
- 노트북에서 `PRIVATE_FOUNDRY_ENDPOINT`를 호출하면 **실패해야** 정상입니다
  (공용 엔드포인트가 닫혀 있습니다).
- 점프박스 VM에서 `nslookup <foundry 계정명>.openai.azure.com` 을 실행하면
  `10.20.1.x` 대역의 사설 IP가 반환되어야 합니다.
- 점프박스 VM에서 `nslookup <ML_WORKSPACE_NAME>.workspace.<리전>.api.azureml.ms` 를 실행하면
  `10.20.3.x` 대역의 사설 IP가 반환되어야 합니다. Foundry와 **다른 서브넷**이라는 점이 핵심입니다.
- 점프박스 브라우저에서 `https://ml.azure.com` 에 접속하면 워크스페이스가 **열려야** 정상입니다.
  같은 주소를 실습자 노트북에서 열면 워크스페이스 목록에는 보이지만 내용은 **열리지 않아야** 정상입니다
  (제어 평면이 Private Endpoint 뒤에 있습니다).
- 점프박스에서는 아무 사이트나 접속됩니다. 이것이 시스템 3과의 차이입니다.

자세한 배경 설명은 [../README.md](../README.md)를 참고하세요.
