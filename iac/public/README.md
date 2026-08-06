# 시스템 1/3 — Public 망

**리소스 그룹:** `rg-<RGBASENAME>-public` · **다른 시스템 의존:** 없음 (언제든 단독 배포/삭제 가능)

실습자 노트북에서 인터넷을 통해 Foundry에 접근하되, **IP 화이트리스트로만** 허용하는 구성이다.

| 항목 | 설정 |
|---|---|
| 인증 | keyless — `disableLocalAuth=true`, Entra ID 토큰 + RBAC |
| 공용 접근 | `publicNetworkAccess=Enabled` |
| 접근 통제 | `networkAcls.defaultAction=Deny` + 노트북 공인 IP + 워크로드 서브넷 |
| NSG | 모든 서브넷에 연결, 우선순위 4096 deny-all 기준선 |

## 배포

```bash
az deployment sub create -n hol01-public -l westus3 \
  --template-file main.bicep \
  --parameters resourceGroupBaseName=hol01 location=westus3 \
               labClientIpAddress="$(curl -s ifconfig.me)" \
               labUserPrincipalId="$(az ad signed-in-user show --query id -o tsv)"
```

또는 이 디렉터리에서 `azd up`.

## 주요 매개변수

| 매개변수 | 기본값 | 설명 |
|---|---|---|
| `labClientIpAddress` | (필수) | 실습자 노트북 공인 IP. 비우면 아무도 접근 못 한다 |
| `labUserPrincipalId` | `''` | 비우면 RBAC 역할 할당을 건너뛴다 → keyless 호출 불가 |
| `vnetAddressPrefix` | `10.10.0.0/16` | private(10.20.0.0/16) · private-whitelist(10.30.0.0/16)와 겹치면 안 된다 |
| `modelDeployments` | `gpt-5.4-mini` | `lifecycleStatus=GenerallyAvailable` 모델만 배포된다 |
| `deployLogAnalytics` | `false` | 이 시스템 전용 작업 영역 생성 여부 |

## 확인

- `SUBNETS_WITHOUT_NSG` 출력이 **비어 있어야** 정상이다.
- 노트북을 다른 네트워크로 옮기면 `403`이 나야 정상이다(화이트리스트가 동작한다는 증거).

자세한 배경은 [../README.md](../README.md) 참고.
