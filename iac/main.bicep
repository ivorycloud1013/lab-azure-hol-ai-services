metadata description = '''
Azure AI Foundry HOL 랜딩존 - Public / Private 이중망 (keyless 인증)

구독 범위에서 리소스 그룹 두 개를 만들고 각각에 존 모듈을 배포한다.

  rg-<env>-public   실습자 노트북 -> 인터넷 -> IP 화이트리스트 -> Foundry
  rg-<env>-private  실습자 노트북 -> Bastion -> 점프박스 -> Private Endpoint -> Foundry

공통 원칙
  - Foundry는 두 존 모두 disableLocalAuth=true. API 키가 존재하지 않고 Entra ID + RBAC만으로 접근한다.
  - VNet에 붙는 모든 서브넷은 NSG를 가지며 우선순위 4096 deny-all이 기본이다.
    (NSG 연결이 불가능한 AzureFirewallSubnet 계열만 예외)
  - Private 망의 아웃바운드는 UDR로 Azure Firewall에 강제 터널링되고 FQDN 화이트리스트로 통제된다.
'''

targetScope = 'subscription'

import { modelDeploymentConfig } from './modules/ai/model-deployments.bicep'

// ---------------------------------------------------------------------------
// 공통
// ---------------------------------------------------------------------------

@description('환경 이름. 리소스 그룹과 리소스 이름의 접두사로 쓰인다.')
@minLength(2)
@maxLength(16)
param environmentName string

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

// ---------------------------------------------------------------------------
// 실습자
// ---------------------------------------------------------------------------

@description('실습자 노트북의 공인 IP. Public Foundry의 IP 화이트리스트에 등록된다. 예: 1.2.3.4 (curl ifconfig.me 로 확인)')
param labClientIpAddress string

@description('keyless 접근 권한을 부여할 실습자 Entra 오브젝트 ID. azd 배포 시 AZURE_PRINCIPAL_ID로 자동 주입된다.')
param labUserPrincipalId string = ''

@description('실습자 보안 주체 유형')
@allowed(['User', 'Group', 'ServicePrincipal'])
param labUserPrincipalType string = 'User'

// ---------------------------------------------------------------------------
// 존 토글
// ---------------------------------------------------------------------------

@description('Public 망을 배포할지 여부')
param deployPublicZone bool = true

@description('Private 망을 배포할지 여부')
param deployPrivateZone bool = true

// ---------------------------------------------------------------------------
// 네트워크
// ---------------------------------------------------------------------------

@description('Public VNet 주소 공간')
param publicVnetAddressPrefix string = '10.10.0.0/16'

@description('Private VNet 주소 공간')
param privateVnetAddressPrefix string = '10.20.0.0/16'

@description('Azure Firewall SKU. Basic이 실습 비용 면에서 가장 저렴하다.')
@allowed(['Basic', 'Standard', 'Premium'])
param firewallSkuTier string = 'Basic'

@description('Azure Bastion SKU')
@allowed(['Basic', 'Standard'])
param bastionSkuName string = 'Basic'

@description('방화벽 FQDN 화이트리스트에 추가할 도메인. 기본 목록을 건드리지 않고 실습 중 확장할 때 쓴다.')
param additionalAllowedFqdns string[] = []

// ---------------------------------------------------------------------------
// 점프박스
// ---------------------------------------------------------------------------

@description('점프박스 관리자 계정 이름')
param vmAdminUsername string = 'azureuser'

@secure()
@minLength(12)
@description('점프박스 관리자 비밀번호. 12자 이상이며 Windows 복잡성 요구사항을 충족해야 한다.')
param vmAdminPassword string

@description('점프박스 VM 크기')
param vmSize string = 'Standard_D2s_v5'

// ---------------------------------------------------------------------------
// 모델
// ---------------------------------------------------------------------------

@description('''
두 존에 동일하게 배포할 모델 목록.
기본값은 위 @allowed 리전 모두에서 lifecycleStatus=GenerallyAvailable + GlobalStandard로 확인된 모델이다.
주의: gpt-4.x 계열은 전 리전에서 Deprecating 상태라 신규 배포가 거부된다.
모델 변경 시 az cognitiveservices model list 로 lifecycleStatus를 먼저 확인할 것.
''')
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

// ---------------------------------------------------------------------------
// 관측 / 거버넌스
// ---------------------------------------------------------------------------

@description('Log Analytics를 배포하고 방화벽/NSG/Foundry 진단 로그를 보낼지 여부. 방화벽 차단 로그 확인에 필요하다.')
param deployLogAnalytics bool = true

@description('"모든 서브넷은 NSG 필수" Azure Policy의 효과. 인프라를 한 번 배포한 뒤 Deny로 올리는 것을 권장한다.')
@allowed(['Audit', 'Deny', 'Disabled'])
param subnetNsgPolicyEffect string = 'Audit'

// ---------------------------------------------------------------------------
// 계산값
// ---------------------------------------------------------------------------

var namePrefix = toLower(environmentName)
var resourceToken = toLower(uniqueString(subscription().id, environmentName, location))

var defaultTags = union(tags, {
  'azd-env-name': environmentName
  workload: 'ai-foundry-hol'
})

var publicResourceGroupName = 'rg-${namePrefix}-public'
var privateResourceGroupName = 'rg-${namePrefix}-private'
var sharedResourceGroupName = 'rg-${namePrefix}-shared'

// ---------------------------------------------------------------------------
// 리소스 그룹
// ---------------------------------------------------------------------------

resource sharedResourceGroup 'Microsoft.Resources/resourceGroups@2025-04-01' = if (deployLogAnalytics) {
  name: sharedResourceGroupName
  location: location
  tags: defaultTags
}

resource publicResourceGroup 'Microsoft.Resources/resourceGroups@2025-04-01' = if (deployPublicZone) {
  name: publicResourceGroupName
  location: location
  tags: defaultTags
}

resource privateResourceGroup 'Microsoft.Resources/resourceGroups@2025-04-01' = if (deployPrivateZone) {
  name: privateResourceGroupName
  location: location
  tags: defaultTags
}

// ---------------------------------------------------------------------------
// 공유 관측 인프라
// ---------------------------------------------------------------------------

module logAnalytics './modules/monitor/log-analytics.bicep' = if (deployLogAnalytics) {
  scope: sharedResourceGroup
  name: 'shared-log-analytics'
  params: {
    name: 'log-${namePrefix}-${resourceToken}'
    location: location
    tags: defaultTags
  }
}

var logAnalyticsWorkspaceId = deployLogAnalytics ? logAnalytics!.outputs.id : ''

// ---------------------------------------------------------------------------
// Public 망
// ---------------------------------------------------------------------------

module publicZone './modules/zones/public-zone.bicep' = if (deployPublicZone) {
  scope: publicResourceGroup
  name: 'zone-public'
  params: {
    namePrefix: namePrefix
    resourceToken: resourceToken
    location: location
    tags: defaultTags
    vnetAddressPrefix: publicVnetAddressPrefix
    workloadSubnetPrefix: cidrSubnet(publicVnetAddressPrefix, 24, 1)
    labClientIpAddress: labClientIpAddress
    labUserPrincipalId: labUserPrincipalId
    labUserPrincipalType: labUserPrincipalType
    modelDeployments: modelDeployments
    logAnalyticsWorkspaceId: logAnalyticsWorkspaceId
  }
}

// ---------------------------------------------------------------------------
// Private 망
// ---------------------------------------------------------------------------

module privateZone './modules/zones/private-zone.bicep' = if (deployPrivateZone) {
  scope: privateResourceGroup
  name: 'zone-private'
  params: {
    namePrefix: namePrefix
    resourceToken: resourceToken
    location: location
    tags: defaultTags
    vnetAddressPrefix: privateVnetAddressPrefix
    firewallSubnetPrefix: cidrSubnet(privateVnetAddressPrefix, 26, 0)
    firewallManagementSubnetPrefix: cidrSubnet(privateVnetAddressPrefix, 26, 1)
    bastionSubnetPrefix: cidrSubnet(privateVnetAddressPrefix, 26, 2)
    privateEndpointSubnetPrefix: cidrSubnet(privateVnetAddressPrefix, 24, 1)
    jumpboxSubnetPrefix: cidrSubnet(privateVnetAddressPrefix, 24, 2)
    firewallSkuTier: firewallSkuTier
    bastionSkuName: bastionSkuName
    additionalAllowedFqdns: additionalAllowedFqdns
    vmSize: vmSize
    vmAdminUsername: vmAdminUsername
    vmAdminPassword: vmAdminPassword
    labUserPrincipalId: labUserPrincipalId
    labUserPrincipalType: labUserPrincipalType
    modelDeployments: modelDeployments
    logAnalyticsWorkspaceId: logAnalyticsWorkspaceId
  }
}

// ---------------------------------------------------------------------------
// 거버넌스 - 모든 서브넷 NSG 필수
// ---------------------------------------------------------------------------

module subnetNsgPolicy './modules/governance/subnet-nsg-policy.bicep' = if (deployPrivateZone && subnetNsgPolicyEffect != 'Disabled') {
  name: 'governance-subnet-nsg-policy'
  params: {
    nameSuffix: namePrefix
    targetResourceGroupName: privateResourceGroupName
    effect: subnetNsgPolicyEffect
  }
  dependsOn: [
    privateZone
  ]
}

// ---------------------------------------------------------------------------
// 출력
// ---------------------------------------------------------------------------

@description('배포 리전')
output AZURE_LOCATION string = location

@description('구독 ID')
output AZURE_SUBSCRIPTION_ID string = subscription().subscriptionId

@description('Public 망 리소스 그룹')
output PUBLIC_RESOURCE_GROUP string = deployPublicZone ? publicResourceGroupName : ''

@description('Private 망 리소스 그룹')
output PRIVATE_RESOURCE_GROUP string = deployPrivateZone ? privateResourceGroupName : ''

@description('공유 리소스 그룹')
output SHARED_RESOURCE_GROUP string = deployLogAnalytics ? sharedResourceGroupName : ''

@description('Public Foundry 엔드포인트. 노트북에서 직접 호출한다.')
output PUBLIC_FOUNDRY_ENDPOINT string = deployPublicZone ? publicZone!.outputs.foundryEndpoint : ''

@description('Public Foundry 프로젝트 이름')
output PUBLIC_FOUNDRY_PROJECT string = deployPublicZone ? publicZone!.outputs.foundryProjectName : ''

@description('Private Foundry 엔드포인트. 점프박스에서만 해석/호출된다.')
output PRIVATE_FOUNDRY_ENDPOINT string = deployPrivateZone ? privateZone!.outputs.foundryEndpoint : ''

@description('Private Foundry 프로젝트 이름')
output PRIVATE_FOUNDRY_PROJECT string = deployPrivateZone ? privateZone!.outputs.foundryProjectName : ''

@description('배포된 모델 배포 이름 목록')
output MODEL_DEPLOYMENT_NAMES array = map(modelDeployments, deployment => deployment.name)

@description('Bastion으로 접속할 점프박스 VM 이름')
output JUMPBOX_NAME string = deployPrivateZone ? privateZone!.outputs.jumpboxName : ''

@description('Azure Bastion 이름')
output BASTION_NAME string = deployPrivateZone ? privateZone!.outputs.bastionName : ''

@description('Azure Firewall 아웃바운드 공인 IP')
output FIREWALL_PUBLIC_IP string = deployPrivateZone ? privateZone!.outputs.firewallPublicIpAddress : ''

@description('UDR next hop 계산값이 실제 방화벽 IP와 일치하는지 여부. false면 강제 터널링이 동작하지 않는다.')
output FIREWALL_ROUTE_IS_VALID bool = deployPrivateZone ? privateZone!.outputs.firewallPrivateIpMatchesRoute : true

@description('Public 망에서 NSG가 없는 서브넷. 비어 있어야 정상이다.')
output PUBLIC_SUBNETS_WITHOUT_NSG array = deployPublicZone ? publicZone!.outputs.subnetsWithoutNsg : []

@description('Private 망에서 NSG가 없는 서브넷. NSG를 지원하지 않는 방화벽 서브넷만 나와야 한다.')
output PRIVATE_SUBNETS_WITHOUT_NSG array = deployPrivateZone ? privateZone!.outputs.subnetsWithoutNsg : []
