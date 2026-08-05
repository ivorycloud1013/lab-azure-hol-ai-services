metadata description = '''
[스택 3/3] Private 망 + URL 화이트리스트 — 독립 배포 단위.

전용 리소스 그룹 rg-<기본이름>-private-whitelist 를 만들고 그 안에서 끝난다.
private 스택과 마찬가지로 VNet·Foundry·VM 한 벌을 자기 것으로 갖되,
점프박스가 접근하는 URL(FQDN)까지 Azure Firewall로 통제한다.

private 스택과의 차이는 딱 세 가지다
  1) VNet에 AzureFirewallSubnet(+Management)을 함께 만든다
  2) 점프박스 서브넷에 0.0.0.0/0 -> 방화벽 UDR을 건다
  3) Firewall Policy(FQDN 화이트리스트)와 Azure Firewall을 배포한다

나머지(NSG deny-all 기준선, 비공개 Foundry, Bastion, 점프박스, keyless RBAC)는
private 스택과 같은 공통 모듈(modules/workload/private-foundry-workload.bicep)을 쓴다.

두 스택은 서로를 참조하지 않는다. 무엇도 공유하지 않으므로 따로 배포·삭제되고,
같은 구독에 나란히 띄워 "URL 통제가 없는 망"과 "있는 망"을 나란히 비교할 수 있다.

UDR next hop
  Azure Firewall은 전용 AzureFirewallSubnet에서 항상 첫 할당 가능 주소(x.x.x.4)를 받는다.
  (.0 네트워크 / .1 게이트웨이 / .2 .3 Azure 예약)
  cidrHost(prefix, 3)으로 미리 계산해 Route Table을 만들고,
  실제 할당값과 일치하는지 FIREWALL_ROUTE_IS_VALID 출력으로 검증한다.
'''

targetScope = 'subscription'

import { modelDeploymentConfig } from '../modules/ai/model-deployments.bicep'

@description('''
리소스 그룹 기본 이름. 최종 이름은 rg-<이 값>-private-whitelist 가 된다.

케이스는 준 그대로 리소스 그룹 이름에 보존된다.
  'RGBASENAME' -> rg-RGBASENAME-private-whitelist
  'rgbasename' -> rg-rgbasename-private-whitelist

단 하위 리소스(VNet, NSG, Foundry, 방화벽 등)의 이름에는 소문자로 변환해 쓴다.
Foundry 엔드포인트가 DNS 이름이라 대문자를 담을 수 없기 때문이다.
''')
@minLength(2)
@maxLength(16)
param resourceGroupBaseName string

@description('배포 리전. 기본 모델의 GA 가용성을 az cognitiveservices model list로 확인한 리전만 허용한다.')
@allowed([
  'westus3'
  'eastus2'
  'swedencentral'
  'koreacentral'
])
param location string = 'westus3'

@description('모든 리소스에 적용할 추가 태그')
param tags object = {}

@description('keyless 접근 권한을 부여할 실습자 Entra 오브젝트 ID. 빈 값이면 실습자 역할 할당을 건너뛴다.')
param labUserPrincipalId string = ''

@description('실습자 보안 주체 유형')
@allowed(['User', 'Group', 'ServicePrincipal'])
param labUserPrincipalType string = 'User'

@description('''
VNet 주소 공간.
다른 스택과 겹치지 않아야 한다 — public 10.10.0.0/16, private 10.20.0.0/16.
''')
param vnetAddressPrefix string = '10.30.0.0/16'

@description('Azure Bastion SKU')
@allowed(['Basic', 'Standard'])
param bastionSkuName string = 'Basic'

@description('Private Endpoint 서브넷에 NSG를 적용할지 여부. Enabled면 NSG가 PE 트래픽에도 적용된다.')
@allowed(['Enabled', 'Disabled'])
param privateEndpointNetworkPolicies string = 'Enabled'

@description('점프박스 VM 크기')
param vmSize string = 'Standard_D2s_v5'

@description('점프박스 관리자 계정 이름')
param vmAdminUsername string = 'azureuser'

@secure()
@minLength(12)
@description('점프박스 관리자 비밀번호. Windows 복잡성 요구사항을 충족해야 한다.')
param vmAdminPassword string

@description('배포할 모델 목록. lifecycleStatus=GenerallyAvailable 인 모델만 배포된다.')
param modelDeployments modelDeploymentConfig[] = [
  {
    name: 'gpt-5.4-mini'
    modelName: 'gpt-5.4-mini'
    modelVersion: '2026-03-17'
    modelFormat: 'OpenAI'
    skuName: 'GlobalStandard'
    capacity: 20
  }
]

@description('''
Azure Firewall SKU.
Basic은 AzureFirewallManagementSubnet과 별도 공인 IP를 추가로 요구하며,
이 스택이 두 가지를 모두 알아서 만든다(private 스택과 맞출 설정이 없다).
''')
@allowed(['Basic', 'Standard', 'Premium'])
param firewallSkuTier string = 'Basic'

@description('위협 인텔리전스 모드. Basic SKU는 Alert까지만 지원한다.')
@allowed(['Off', 'Alert', 'Deny'])
param threatIntelMode string = 'Alert'

// ---------------------------------------------------------------------------
// 화이트리스트 본체 — 점프박스가 어떤 URL로 나갈 수 있는지
// ---------------------------------------------------------------------------

@description('L4로 허용할 Azure 서비스 태그 목록')
param allowedServiceTags string[] = [
  'AzureActiveDirectory'
  'AzureResourceManager'
  'AzureMonitor'
  'AzureFrontDoor.FirstParty'
]

@description('Entra ID 인증 및 Azure 관리 평면 FQDN 화이트리스트')
param identityAndManagementFqdns string[] = [
  'login.microsoftonline.com'
  'login.microsoft.com'
  'login.windows.net'
  '*.login.microsoftonline.com'
  'management.azure.com'
  'graph.microsoft.com'
  '*.msauth.net'
  '*.msftauth.net'
  '*.aadcdn.msftauth.net'
]

@description('Azure AI Foundry 데이터/제어 평면 FQDN 화이트리스트')
param foundryFqdns string[] = [
  'ai.azure.com'
  '*.ai.azure.com'
  '*.services.ai.azure.com'
  '*.openai.azure.com'
  '*.cognitiveservices.azure.com'
  '*.azureml.ms'
  '*.api.azureml.ms'
]

@description('Azure Portal FQDN 화이트리스트')
param portalFqdns string[] = [
  'portal.azure.com'
  '*.portal.azure.com'
  '*.portal.azure.net'
  '*.hosting.portal.azure.net'
  '*.ext.azure.com'
]

@description('실습 도구(Python 패키지 등) FQDN 화이트리스트')
param toolingFqdns string[] = [
  'pypi.org'
  '*.pypi.org'
  'files.pythonhosted.org'
  'aka.ms'
  '*.azureedge.net'
]

@description('실습 중 추가로 열어야 하는 FQDN. 기본 목록을 건드리지 않고 확장할 때 사용한다.')
param additionalAllowedFqdns string[] = []

@description('Windows Update 등 Azure Firewall FQDN 태그로 허용할 목록')
param allowedFqdnTags string[] = [
  'WindowsUpdate'
  'WindowsDiagnostics'
  'MicrosoftActiveProtectionService'
]

@description('''
이 스택 전용 Log Analytics 작업 영역을 만들지 여부.
방화벽이 무엇을 막고 무엇을 통과시켰는지 보려면 필요하므로 기본값이 true다.
''')
param deployLogAnalytics bool = true

@description('기존 Log Analytics 작업 영역 ID. deployLogAnalytics=false 일 때만 사용된다.')
param existingLogAnalyticsWorkspaceId string = ''

@description('"모든 서브넷은 NSG 필수" Azure Policy의 효과. 인프라를 한 번 배포한 뒤 Deny로 올리는 것을 권장한다.')
@allowed(['Audit', 'Deny', 'Disabled'])
param subnetNsgPolicyEffect string = 'Audit'

var STACK_SUFFIX = 'private-whitelist'

var namePrefix = toLower(resourceGroupBaseName)
var resourceToken = toLower(uniqueString(subscription().id, resourceGroupBaseName, location, STACK_SUFFIX))

var defaultTags = union(tags, {
  'azd-env-name': resourceGroupBaseName
  workload: 'ai-foundry-hol'
  stack: STACK_SUFFIX
})

// 케이스를 보존하기 위해 namePrefix(소문자)가 아니라 원본 값을 쓴다.
var resourceGroupName = 'rg-${resourceGroupBaseName}-${STACK_SUFFIX}'

// Basic SKU만 관리 서브넷과 관리용 공인 IP를 요구한다.
var requiresManagementSubnet = firewallSkuTier == 'Basic'

var firewallSubnetPrefix = cidrSubnet(vnetAddressPrefix, 26, 0)
var firewallManagementSubnetPrefix = cidrSubnet(vnetAddressPrefix, 26, 1)
var bastionSubnetPrefix = cidrSubnet(vnetAddressPrefix, 26, 2)
var privateEndpointSubnetPrefix = cidrSubnet(vnetAddressPrefix, 24, 1)
var jumpboxSubnetPrefix = cidrSubnet(vnetAddressPrefix, 24, 2)

resource whitelistResourceGroup 'Microsoft.Resources/resourceGroups@2025-04-01' = {
  name: resourceGroupName
  location: location
  tags: defaultTags
}

module logAnalytics '../modules/monitor/log-analytics.bicep' = if (deployLogAnalytics) {
  scope: whitelistResourceGroup
  name: 'private-whitelist-log-analytics'
  params: {
    name: 'log-${namePrefix}-${STACK_SUFFIX}-${resourceToken}'
    location: location
    tags: defaultTags
  }
}

var logAnalyticsWorkspaceId = deployLogAnalytics ? logAnalytics!.outputs.id : existingLogAnalyticsWorkspaceId

module resources './resources.bicep' = {
  scope: whitelistResourceGroup
  name: 'private-whitelist-resources'
  params: {
    namePrefix: namePrefix
    nameSuffix: STACK_SUFFIX
    resourceToken: resourceToken
    location: location
    tags: defaultTags
    vnetAddressPrefix: vnetAddressPrefix
    firewallSubnetPrefix: firewallSubnetPrefix
    firewallManagementSubnetPrefix: firewallManagementSubnetPrefix
    deployFirewallManagementSubnet: requiresManagementSubnet
    bastionSubnetPrefix: bastionSubnetPrefix
    privateEndpointSubnetPrefix: privateEndpointSubnetPrefix
    jumpboxSubnetPrefix: jumpboxSubnetPrefix
    bastionSkuName: bastionSkuName
    privateEndpointNetworkPolicies: privateEndpointNetworkPolicies
    vmSize: vmSize
    vmAdminUsername: vmAdminUsername
    vmAdminPassword: vmAdminPassword
    labUserPrincipalId: labUserPrincipalId
    labUserPrincipalType: labUserPrincipalType
    modelDeployments: modelDeployments
    firewallSkuTier: firewallSkuTier
    threatIntelMode: threatIntelMode
    allowedServiceTags: allowedServiceTags
    identityAndManagementFqdns: identityAndManagementFqdns
    foundryFqdns: foundryFqdns
    portalFqdns: portalFqdns
    toolingFqdns: toolingFqdns
    additionalAllowedFqdns: additionalAllowedFqdns
    allowedFqdnTags: allowedFqdnTags
    logAnalyticsWorkspaceId: logAnalyticsWorkspaceId
  }
}

module subnetNsgPolicy '../modules/governance/subnet-nsg-policy.bicep' = if (subnetNsgPolicyEffect != 'Disabled') {
  name: 'private-whitelist-subnet-nsg-policy'
  params: {
    nameSuffix: '${namePrefix}-${STACK_SUFFIX}'
    targetResourceGroupName: resourceGroupName
    effect: subnetNsgPolicyEffect
  }
  dependsOn: [
    resources
  ]
}

// ---------------------------------------------------------------------------
// 출력
// ---------------------------------------------------------------------------

@description('배포 리전')
output AZURE_LOCATION string = location

@description('이 스택의 리소스 그룹')
output PRIVATE_WHITELIST_RESOURCE_GROUP string = resourceGroupName

@description('VNet 이름')
output PRIVATE_WHITELIST_VNET_NAME string = resources.outputs.vnetName

@description('Foundry 계정 이름')
output PRIVATE_WHITELIST_FOUNDRY_ACCOUNT string = resources.outputs.foundryAccountName

@description('Foundry 엔드포인트. 점프박스에서만 해석/호출된다.')
output PRIVATE_WHITELIST_FOUNDRY_ENDPOINT string = resources.outputs.foundryEndpoint

@description('Foundry 프로젝트 이름')
output PRIVATE_WHITELIST_FOUNDRY_PROJECT string = resources.outputs.foundryProjectName

@description('배포된 모델 배포 이름 목록')
output MODEL_DEPLOYMENT_NAMES array = map(modelDeployments, deployment => deployment.name)

@description('Bastion으로 접속할 점프박스 VM 이름')
output JUMPBOX_NAME string = resources.outputs.jumpboxName

@description('점프박스 사설 IP')
output JUMPBOX_PRIVATE_IP string = resources.outputs.jumpboxPrivateIpAddress

@description('Azure Bastion 이름')
output BASTION_NAME string = resources.outputs.bastionName

@description('Azure Firewall 이름')
output FIREWALL_NAME string = resources.outputs.firewallName

@description('Azure Firewall 사설 IP. 점프박스 Route Table의 next hop이다.')
output FIREWALL_PRIVATE_IP string = resources.outputs.firewallPrivateIpAddress

@description('Azure Firewall 아웃바운드 공인 IP (SNAT 주소)')
output FIREWALL_PUBLIC_IP string = resources.outputs.firewallPublicIpAddress

@description('Firewall Policy 이름')
output FIREWALL_POLICY_NAME string = resources.outputs.firewallPolicyName

@description('''
실제 방화벽 IP가 Route Table의 next hop과 일치하는지 여부.
false면 강제 터널링이 동작하지 않으므로 아웃바운드가 블랙홀이 된다.
''')
output FIREWALL_ROUTE_IS_VALID bool = resources.outputs.firewallPrivateIpAddress == resources.outputs.routeNextHopIpAddress

@description('화이트리스트에 등록된 FQDN 총 개수')
output ALLOWED_FQDN_COUNT int = length(identityAndManagementFqdns) + length(foundryFqdns) + length(portalFqdns) + length(toolingFqdns) + length(additionalAllowedFqdns)

@description('방화벽 로그를 조회할 Log Analytics 작업 영역 ID')
output LOG_ANALYTICS_WORKSPACE_ID string = logAnalyticsWorkspaceId

@description('NSG가 연결되지 않은 서브넷 목록. NSG를 지원하지 않는 방화벽 서브넷만 나와야 한다.')
output SUBNETS_WITHOUT_NSG array = resources.outputs.subnetsWithoutNsg
