# 스택 3/3 — 화이트리스트 (Azure Firewall)

**리소스 그룹:** `rg-<env>-whitelist` · **의존:** [스택 2 private](../private/)를 **먼저** 배포해야 한다

"무엇을 뚫어줄 것인가"만 담당하는 스택이다. private 망을 건드리지 않고 규칙만 갱신·재배포할 수 있고,
이 스택만 지우면 private 망이 즉시 완전 차단 상태로 돌아간다.

## private 스택과의 접점

Azure Firewall은 반드시 보호 대상 VNet의 `AzureFirewallSubnet` 안에 있어야 한다.
그 서브넷은 private 스택이 만들어 두고, 여기서는 **이름으로 참조만** 한다.
방화벽 리소스 자체는 이 스택의 리소스 그룹에 생성된다(서로 다른 RG여도 무방하다).

## 규칙 구조

| 우선순위 | 규칙 그룹 | 내용 |
|---|---|---|
| 200 | `rcg-network-allow` | L3/L4 서비스 태그 화이트리스트 (Entra ID, ARM, Monitor 등) |
| 300 | `rcg-application-allow` | L7 FQDN 화이트리스트 (Foundry, 포털, 패키지 저장소, Windows Update) |
| 350 | `rcg-application-allow-additional` | `additionalAllowedFqdns` 확장분 |
| 400 | `rcg-deny-all` | 명시적 Deny-All. 방화벽 기본도 deny지만 로그에 남기려고 둔다 |

## 배포

```bash
PRV=$(az deployment sub show -n hol01-private --query properties.outputs -o json)

az deployment sub create -n hol01-whitelist -l westus3 \
  --template-file main.bicep \
  --parameters resourceGroupBaseName=hol01 location=westus3 \
               privateVnetResourceGroupName=$(echo $PRV | jq -r .PRIVATE_RESOURCE_GROUP.value) \
               privateVnetName=$(echo $PRV | jq -r .PRIVATE_VNET_NAME.value) \
               expectedFirewallPrivateIp=$(echo $PRV | jq -r .EXPECTED_FIREWALL_PRIVATE_IP.value)
```

## 화이트리스트 확장

```bash
az deployment sub create -n hol01-whitelist -l westus3 \
  --template-file main.bicep \
  --parameters resourceGroupBaseName=hol01 location=westus3 \
               privateVnetResourceGroupName=rg-hol01-private \
               privateVnetName=vnet-hol01-private \
               additionalAllowedFqdns='["github.com","*.githubusercontent.com"]'
```

허용 목록 매개변수: `identityAndManagementFqdns` / `foundryFqdns` / `portalFqdns` / `toolingFqdns` /
`additionalAllowedFqdns` / `allowedServiceTags` / `allowedFqdnTags`

## 확인

**`FIREWALL_ROUTE_IS_VALID` 가 `true` 여야 한다.**

```bash
az deployment sub show -n hol01-whitelist --query properties.outputs.FIREWALL_ROUTE_IS_VALID.value
```

`false`면 실제 방화벽 IP가 private 스택 Route Table의 next hop과 어긋난 상태이며, 강제 터널링이 동작하지 않는다.

차단 로그 확인:

```kusto
AZFWApplicationRule
| where TimeGenerated > ago(30m)
| project TimeGenerated, SourceIp, Fqdn, Action, Rule
| order by TimeGenerated desc
```

## 주의

- `firewallSkuTier`(기본 `Basic`)는 private 스택의 `deployFirewallManagementSubnet`과 **맞아야 한다.**
  Basic은 `AzureFirewallManagementSubnet`과 별도 공인 IP를 요구한다.
- Basic SKU는 위협 인텔리전스가 `Alert`까지만 지원한다.
- 이 스택은 시간당 약 **$0.41** 과금된다(Firewall Basic + 공인 IP 2개). 실습을 쉴 때 이 스택만 지우면 절약된다.

자세한 배경은 [../README.md](../README.md) 참고.
