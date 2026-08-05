# lab-azure-hol-ai-services

Azure AI Foundry를 **Public 망 / Private 망** 두 벌로 배포하고, 두 망의 접근 통제 차이를
직접 비교해 보는 핸즈온 랩(HOL)이다. 인증은 양쪽 모두 **keyless(Entra ID + RBAC)** 로 구성한다.

| | Public 망 | Private 망 |
|---|---|---|
| 접근 경로 | 실습자 노트북 → 인터넷 | 실습자 노트북 → Bastion → 점프박스 VM |
| Foundry 노출 | `publicNetworkAccess=Enabled` + IP 화이트리스트 | `publicNetworkAccess=Disabled` + Private Endpoint |
| Azure 서비스 우회 | `bypass=AzureServices` | `bypass=None` (Azure도 차단) |
| 아웃바운드 | 제한 없음 | UDR 강제 터널링 → Azure Firewall FQDN 화이트리스트 |
| NSG | 모든 서브넷 + deny-all 기본 | 모든 서브넷 + deny-all 기본 |
| 인증 | keyless (`disableLocalAuth=true`) | keyless (`disableLocalAuth=true`) |

## 빠른 시작

```bash
az login
azd env new hol01
azd env set LAB_CLIENT_IP     "$(curl -s ifconfig.me)"
azd env set VM_ADMIN_PASSWORD '<12자 이상 복잡한 비밀번호>'
azd up
```

기본 리전은 **westus3**, 기본 모델은 **gpt-5.4-mini**다.

## 문서

- **[iac/README.md](iac/README.md)** — 아키텍처, 배포 절차, 실습 시나리오, 설계 결정과 제약, 비용, 정리 방법

## 비용 주의

Azure Firewall과 Bastion은 **유휴 상태에서도 시간당 과금**된다. 상시 과금 합계는 약 **$0.79/시간**이다.
실습이 끝나면 `azd down --purge`로 반드시 정리한다.
