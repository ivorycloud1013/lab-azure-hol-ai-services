# 추가 리전 Foundry

**리소스 그룹:** 기존 `rg-<RGBASENAME>-<시스템>` 을 그대로 씀 · **의존:** 대상 시스템이 먼저 배포돼 있어야 한다

리소스 그룹은 리전에 묶이지 않습니다. 그래서 이미 만들어 둔 public · private ·
private-whitelist 리소스 그룹 안에 **다른 리전의 Foundry 계정**을 덧붙일 수 있습니다.
모델이 특정 리전에만 있을 때, 또는 리전별 지연 시간을 비교할 때 씁니다.

네트워크는 새로 만들지 않습니다. 기존 시스템이 만든 VNet, 서브넷,
Private DNS Zone을 찾아서 재사용합니다.

## 배포

```bash
cd iac
make deploy-foundry-region TARGET_SYSTEM=private FOUNDRY_REGION=swedencentral
```

| 인자 | 설명 |
|---|---|
| `TARGET_SYSTEM` | `public` · `private` · `private-whitelist` 중 하나. 이미 배포된 시스템이어야 한다 |
| `FOUNDRY_REGION` | 추가할 Foundry의 리전 |
| `RGBASENAME` | 대상 시스템을 배포할 때 쓴 값과 같아야 한다 |

## 만들어지는 것

| 대상 시스템 | 만드는 리소스 | 접근 경로 |
|---|---|---|
| `public` | Foundry 계정 · 프로젝트 · 모델 배포 · 역할 할당 | IP 화이트리스트 |
| `private` · `private-whitelist` | 위 + **Private Endpoint** | Private Endpoint |

Private Endpoint는 기존 VNet의 `snet-private-endpoint`에 만들어지고, 기존
Private DNS Zone 세 개에 A 레코드를 등록합니다. Private Endpoint는 리전 간
연결을 지원하므로, 엔드포인트는 VNet 리전에 있고 계정은 다른 리전에 있어도 됩니다.

## 이름 규칙

계정 이름에 리전이 들어가서 같은 리소스 그룹 안에서 충돌하지 않습니다.

```
aif-<RGBASENAME>-<약어>-<리전>-<토큰>
예: aif-hol01-priv-swedencentral-mr4e32cr4kq2i
```

약어는 `public`→`pub`, `private`→`priv`, `private-whitelist`→`privwl` 입니다.
계정 이름은 64자를 넘을 수 없고 전역 고유 DNS 이름이기도 해서, 긴 시스템 이름 대신
약어를 씁니다.

## 알아 둘 점

**public 대상은 IP 화이트리스트로만 통제됩니다.** 기본 public 시스템은 IP 화이트리스트에
더해 워크로드 서브넷의 서비스 엔드포인트 규칙도 걸지만, 서비스 엔드포인트 기반 VNet
규칙은 서브넷과 계정이 **같은 리전일 때만** 동작합니다. 추가 리전 Foundry는 정의상
리전이 다르므로 이 규칙을 걸지 않습니다.

**모델 가용성은 리전마다 다릅니다.** 기본값은 `gpt-5.4-mini`이며, 해당 리전에 없으면
배포가 실패합니다. 먼저 확인하세요.

```bash
az cognitiveservices model list -l <리전> \
  --query "[?model.name=='gpt-5.4-mini'].{name:model.name,version:model.version,status:model.lifecycleStatus}" -o table
```

**대상 시스템과 같은 리전을 넣어도 막지 않습니다.** 토큰이 달라져 별개의 계정이 하나 더
생길 뿐입니다. 의도한 것이 아니라면 리전 값을 확인하세요.

## 확인

```bash
make outputs-foundry-region TARGET_SYSTEM=private FOUNDRY_REGION=swedencentral
```

`ACCESS_PATH`가 `Private Endpoint`인지 `IP whitelist`인지로 접근 경로를 확인할 수 있습니다.

자세한 배경은 [../README.md](../README.md) 참고.
