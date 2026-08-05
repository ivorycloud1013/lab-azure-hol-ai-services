metadata description = '''
[스택 3/3] 화이트리스트 — 독립 배포 단위.

전용 리소스 그룹 rg-<env>-whitelist 에 Azure Firewall Policy와 Azure Firewall을 배포한다.
"무엇을 뚫어줄 것인가"라는 관심사만 이 스택이 소유하므로,
private 스택을 건드리지 않고 화이트리스트만 갱신·재배포할 수 있다.

private 스택과의 유일한 접점
  Azure Firewall은 반드시 보호 대상 VNet의 AzureFirewallSubnet 안에 있어야 한다.
  그 서브넷은 private 스택이 미리 만들어 두므로, 여기서는 이름으로 참조만 한다.
  (방화벽 리소스 자체는 이 스택의 리소스 그룹에 생성된다 — 서로 다른 RG여도 무방하다.)

  따라서 배포 순서는 private -> whitelist 다.

검증
  private 스택의 Route Table은 방화벽 IP를 계산값으로 미리 잡아 두었다.
  이 스택은 실제로 할당된 방화벽 IP가 그 값과 같은지 FIREWALL_ROUTE_IS_VALID 로 확인한다.
  false 라면 강제 터널링이 끊긴 상태이므로 즉시 조치해야 한다.
'''

targetScope = 'subscription'

@description('환경 이름. 리소스 그룹과 리소스 이름의 접두사로 쓰인다. private 스택과 같은 값을 써야 한다.')
@minLength(2)
@maxLength(16)
param environmentName string

@description('배포 리전. private 스택과 같은 리전이어야 한다.')
@allowed([
  'westus3'
  'eastus2'
  'swedencentral'
  'koreacentral'
])
param location string = 'westus3'

@description('모든 리소스에 적용할 추가 태그')
param tags object = {}

@description('보호할 VNet이 있는 리소스 그룹. private 스택의 PRIVATE_RESOURCE_GROUP 출력값.')
param privateVnetResourceGroupName string

@description('보호할 VNet 이름. private 스택의 PRIVATE_VNET_NAME 출력값.')
param privateVnetName string

@description('방화벽 규칙의 출발지 CIDR 목록. private 스택의 PROTECTED_SOURCE_ADDRESSES 출력값.')
param protectedSourceAddresses string[] = ['10.20.2.0/24']

@description('''
private 스택의 Route Table이 기대하는 방화벽 사설 IP.
private 스택의 EXPECTED_FIREWALL_PRIVATE_IP 출력값을 넘기면 배포 후 자동 검증된다.
빈 값이면 검증을 건너뛴다.
''')
param expectedFirewallPrivateIp string = ''

@description('''
Azure Firewall SKU.
Basic은 AzureFirewallManagementSubnet과 별도 공인 IP를 추가로 요구한다.
private 스택의 deployFirewallManagementSubnet 값과 맞아야 한다.
''')
@allowed(['Basic', 'Standard', 'Premium'])
param firewallSkuTier string = 'Basic'

// ---------------------------------------------------------------------------
// 화이트리스트 본체 — 이 스택의 존재 이유
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

@description('위협 인텔리전스 모드. Basic SKU는 Alert만 지원한다.')
@allowed(['Off', 'Alert', 'Deny'])
param threatIntelMode string = 'Alert'

@description('''
이 스택 전용 Log Analytics 작업 영역을 만들지 여부.
방화벽이 무엇을 막고 무엇을 통과시켰는지 보려면 필요하므로 기본값이 true다.
''')
param deployLogAnalytics bool = true

@description('기존 Log Analytics 작업 영역 ID. deployLogAnalytics=false 일 때만 사용된다.')
param existingLogAnalyticsWorkspaceId string = ''

var namePrefix = toLower(environmentName)
var resourceToken = toLower(uniqueString(subscription().id, environmentName, location))

var defaultTags = union(tags, {
  'azd-env-name': environmentName
  workload: 'ai-foundry-hol'
  stack: 'whitelist'
})

var resourceGroupName = 'rg-${namePrefix}-whitelist'

var requiresManagementSubnet = firewallSkuTier == 'Basic'

// private 스택이 만들어 둔 서브넷을 이름으로 참조한다.
var firewallSubnetId = resourceId(
  subscription().subscriptionId,
  privateVnetResourceGroupName,
  'Microsoft.Network/virtualNetworks/subnets',
  privateVnetName,
  'AzureFirewallSubnet'
)
var firewallManagementSubnetId = resourceId(
  subscription().subscriptionId,
  privateVnetResourceGroupName,
  'Microsoft.Network/virtualNetworks/subnets',
  privateVnetName,
  'AzureFirewallManagementSubnet'
)

resource whitelistResourceGroup 'Microsoft.Resources/resourceGroups@2025-04-01' = {
  name: resourceGroupName
  location: location
  tags: defaultTags
}

module logAnalytics '../modules/monitor/log-analytics.bicep' = if (deployLogAnalytics) {
  scope: whitelistResourceGroup
  name: 'whitelist-log-analytics'
  params: {
    name: 'log-${namePrefix}-whitelist-${resourceToken}'
    location: location
    tags: defaultTags
  }
}

var logAnalyticsWorkspaceId = deployLogAnalytics ? logAnalytics!.outputs.id : existingLogAnalyticsWorkspaceId

module resources './resources.bicep' = {
  scope: whitelistResourceGroup
  name: 'whitelist-resources'
  params: {
    namePrefix: namePrefix
    location: location
    tags: defaultTags
    firewallSkuTier: firewallSkuTier
    firewallSubnetId: firewallSubnetId
    firewallManagementSubnetId: requiresManagementSubnet ? firewallManagementSubnetId : ''
    protectedSourceAddresses: protectedSourceAddresses
    allowedServiceTags: allowedServiceTags
    identityAndManagementFqdns: identityAndManagementFqdns
    foundryFqdns: foundryFqdns
    portalFqdns: portalFqdns
    toolingFqdns: toolingFqdns
    additionalAllowedFqdns: additionalAllowedFqdns
    allowedFqdnTags: allowedFqdnTags
    threatIntelMode: threatIntelMode
    logAnalyticsWorkspaceId: logAnalyticsWorkspaceId
  }
}

@description('배포 리전')
output AZURE_LOCATION string = location

@description('화이트리스트 리소스 그룹')
output WHITELIST_RESOURCE_GROUP string = resourceGroupName

@description('Azure Firewall 이름')
output FIREWALL_NAME string = resources.outputs.firewallName

@description('Azure Firewall 사설 IP. private 스택 Route Table의 next hop이어야 한다.')
output FIREWALL_PRIVATE_IP string = resources.outputs.firewallPrivateIpAddress

@description('Azure Firewall 아웃바운드 공인 IP (SNAT 주소)')
output FIREWALL_PUBLIC_IP string = resources.outputs.firewallPublicIpAddress

@description('''
실제 방화벽 IP가 private 스택의 Route Table next hop과 일치하는지 여부.
false면 강제 터널링이 동작하지 않으므로 private 스택의 Route Table을 고쳐야 한다.
expectedFirewallPrivateIp를 넘기지 않으면 검증을 건너뛰고 true를 반환한다.
''')
output FIREWALL_ROUTE_IS_VALID bool = empty(expectedFirewallPrivateIp)
  ? true
  : resources.outputs.firewallPrivateIpAddress == expectedFirewallPrivateIp

@description('Firewall Policy 이름')
output FIREWALL_POLICY_NAME string = resources.outputs.firewallPolicyName

@description('방화벽 로그를 조회할 Log Analytics 작업 영역 ID')
output LOG_ANALYTICS_WORKSPACE_ID string = logAnalyticsWorkspaceId

@description('화이트리스트에 등록된 FQDN 총 개수')
output ALLOWED_FQDN_COUNT int = length(identityAndManagementFqdns) + length(foundryFqdns) + length(portalFqdns) + length(toolingFqdns) + length(additionalAllowedFqdns)
