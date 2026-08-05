metadata description = '''
[스택 2/3] Private 망 — 독립 배포 단위.

전용 리소스 그룹 rg-<env>-private 을 만들고 그 안에 네트워크와 워크로드를 배포한다.
방화벽 자체는 이 스택에 없다. 화이트리스트는 [스택 3/3] whitelist 가 소유한다.

이 스택이 만드는 것
  - VNet (AzureFirewallSubnet 자리를 비워 둔 채로 함께 정의)
  - 모든 서브넷의 NSG (우선순위 4096 deny-all 기준선)
  - 0.0.0.0/0 -> 방화벽으로 향하는 Route Table
  - Azure Bastion, 점프박스 VM
  - Foundry (publicNetworkAccess=Disabled, bypass=None) + Private Endpoint + Private DNS
  - "모든 서브넷은 NSG 필수" Azure Policy

whitelist 스택과의 관계
  Route Table의 next hop은 방화벽이 받게 될 IP를 cidrHost로 미리 계산한 값이다.
  따라서 이 스택은 방화벽 없이도 단독 배포된다. 다만 그 상태에서는 아웃바운드가
  블랙홀이 되고, whitelist 스택을 배포하는 순간 화이트리스트대로 뚫린다.
  (Bastion 서브넷에는 UDR을 걸지 않으므로 방화벽 없이도 접속은 된다.)
'''

targetScope = 'subscription'

import { modelDeploymentConfig } from '../modules/ai/model-deployments.bicep'

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

@description('keyless 접근 권한을 부여할 실습자 Entra 오브젝트 ID. 빈 값이면 실습자 역할 할당을 건너뛴다.')
param labUserPrincipalId string = ''

@description('실습자 보안 주체 유형')
@allowed(['User', 'Group', 'ServicePrincipal'])
param labUserPrincipalType string = 'User'

@description('Private VNet 주소 공간. public 스택(10.10.0.0/16)과 겹치지 않아야 한다.')
param privateVnetAddressPrefix string = '10.20.0.0/16'

@description('''
AzureFirewallManagementSubnet을 함께 만들지 여부.
whitelist 스택에서 Azure Firewall Basic SKU를 쓰면 필수다. Standard/Premium이면 false로 둔다.
''')
param deployFirewallManagementSubnet bool = true

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

@description('이 스택 전용 Log Analytics 작업 영역을 만들지 여부')
param deployLogAnalytics bool = false

@description('기존 Log Analytics 작업 영역 ID. deployLogAnalytics=false 일 때만 사용된다.')
param existingLogAnalyticsWorkspaceId string = ''

@description('"모든 서브넷은 NSG 필수" Azure Policy의 효과. 인프라를 한 번 배포한 뒤 Deny로 올리는 것을 권장한다.')
@allowed(['Audit', 'Deny', 'Disabled'])
param subnetNsgPolicyEffect string = 'Audit'

var namePrefix = toLower(environmentName)
var resourceToken = toLower(uniqueString(subscription().id, environmentName, location))

var defaultTags = union(tags, {
  'azd-env-name': environmentName
  workload: 'ai-foundry-hol'
  stack: 'private'
})

var resourceGroupName = 'rg-${namePrefix}-private'

var firewallSubnetPrefix = cidrSubnet(privateVnetAddressPrefix, 26, 0)
var firewallManagementSubnetPrefix = cidrSubnet(privateVnetAddressPrefix, 26, 1)
var bastionSubnetPrefix = cidrSubnet(privateVnetAddressPrefix, 26, 2)
var privateEndpointSubnetPrefix = cidrSubnet(privateVnetAddressPrefix, 24, 1)
var jumpboxSubnetPrefix = cidrSubnet(privateVnetAddressPrefix, 24, 2)

resource privateResourceGroup 'Microsoft.Resources/resourceGroups@2025-04-01' = {
  name: resourceGroupName
  location: location
  tags: defaultTags
}

module logAnalytics '../modules/monitor/log-analytics.bicep' = if (deployLogAnalytics) {
  scope: privateResourceGroup
  name: 'private-log-analytics'
  params: {
    name: 'log-${namePrefix}-private-${resourceToken}'
    location: location
    tags: defaultTags
  }
}

var logAnalyticsWorkspaceId = deployLogAnalytics ? logAnalytics!.outputs.id : existingLogAnalyticsWorkspaceId

module resources './resources.bicep' = {
  scope: privateResourceGroup
  name: 'private-resources'
  params: {
    namePrefix: namePrefix
    resourceToken: resourceToken
    location: location
    tags: defaultTags
    vnetAddressPrefix: privateVnetAddressPrefix
    firewallSubnetPrefix: firewallSubnetPrefix
    firewallManagementSubnetPrefix: firewallManagementSubnetPrefix
    deployFirewallManagementSubnet: deployFirewallManagementSubnet
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
    logAnalyticsWorkspaceId: logAnalyticsWorkspaceId
  }
}

module subnetNsgPolicy '../modules/governance/subnet-nsg-policy.bicep' = if (subnetNsgPolicyEffect != 'Disabled') {
  name: 'private-subnet-nsg-policy'
  params: {
    nameSuffix: namePrefix
    targetResourceGroupName: resourceGroupName
    effect: subnetNsgPolicyEffect
  }
  dependsOn: [
    resources
  ]
}

// ---------------------------------------------------------------------------
// 출력 - 앞의 5개는 whitelist 스택의 입력 매개변수로 그대로 넘긴다
// ---------------------------------------------------------------------------

@description('[whitelist 스택 입력] Private VNet이 속한 리소스 그룹')
output PRIVATE_RESOURCE_GROUP string = resourceGroupName

@description('[whitelist 스택 입력] Private VNet 이름')
output PRIVATE_VNET_NAME string = resources.outputs.vnetName

@description('[whitelist 스택 입력] 방화벽이 보호할 출발지 CIDR (점프박스 서브넷)')
output PROTECTED_SOURCE_ADDRESSES array = [jumpboxSubnetPrefix]

@description('[whitelist 스택 입력] Route Table이 기대하는 방화벽 사설 IP. whitelist 스택이 이 값과 일치하는지 검증한다.')
output EXPECTED_FIREWALL_PRIVATE_IP string = resources.outputs.routeNextHopIpAddress

@description('[whitelist 스택 입력] AzureFirewallManagementSubnet 생성 여부. whitelist 스택의 SKU 선택과 맞아야 한다.')
output FIREWALL_MANAGEMENT_SUBNET_DEPLOYED bool = deployFirewallManagementSubnet

@description('배포 리전')
output AZURE_LOCATION string = location

@description('Private Foundry 계정 이름')
output PRIVATE_FOUNDRY_ACCOUNT string = resources.outputs.foundryAccountName

@description('Private Foundry 엔드포인트. 점프박스에서만 해석/호출된다.')
output PRIVATE_FOUNDRY_ENDPOINT string = resources.outputs.foundryEndpoint

@description('Private Foundry 프로젝트 이름')
output PRIVATE_FOUNDRY_PROJECT string = resources.outputs.foundryProjectName

@description('배포된 모델 배포 이름 목록')
output MODEL_DEPLOYMENT_NAMES array = map(modelDeployments, deployment => deployment.name)

@description('Bastion으로 접속할 점프박스 VM 이름')
output JUMPBOX_NAME string = resources.outputs.jumpboxName

@description('점프박스 사설 IP')
output JUMPBOX_PRIVATE_IP string = resources.outputs.jumpboxPrivateIpAddress

@description('Azure Bastion 이름')
output BASTION_NAME string = resources.outputs.bastionName

@description('NSG가 연결되지 않은 서브넷 목록. NSG를 지원하지 않는 방화벽 서브넷만 나와야 한다.')
output SUBNETS_WITHOUT_NSG array = resources.outputs.subnetsWithoutNsg
