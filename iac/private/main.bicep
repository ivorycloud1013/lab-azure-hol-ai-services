metadata description = '''
[스택 2/3] Private 망 — 독립 배포 단위.

전용 리소스 그룹 rg-<기본이름>-private 을 만들고 그 안에서 끝난다.
다른 스택과 어떤 리소스도 공유하지 않으며, 배포 순서 제약도 없다.

이 스택이 만드는 것 — VNet 하나에 Foundry와 VM만 들어간다
  - VNet (Bastion / Private Endpoint / 점프박스 서브넷)
  - 모든 서브넷의 NSG (우선순위 4096 deny-all 기준선)
  - Azure Bastion, 점프박스 VM
  - Foundry (publicNetworkAccess=Disabled, bypass=None) + Private Endpoint + Private DNS
  - "모든 서브넷은 NSG 필수" Azure Policy

접근 경로: 실습자 노트북 -> Bastion -> 점프박스 VM -> Private Endpoint -> Foundry

이 스택에 방화벽은 없다
  점프박스의 아웃바운드는 NSG(대역·포트)까지만 통제되고 인터넷으로 직접 나간다.
  URL(FQDN) 단위로 통제하려면 [스택 3/3] private-whitelist 를 쓴다.
  그 스택은 이 스택과 독립된 자기 VNet·Foundry·VM 한 벌을 따로 갖는다.
'''

targetScope = 'subscription'

import { modelDeploymentConfig } from '../modules/ai/model-deployments.bicep'

@description('''
리소스 그룹 기본 이름. 최종 이름은 rg-<이 값>-private 이 된다.

케이스는 준 그대로 리소스 그룹 이름에 보존된다.
  'RGBASENAME' -> rg-RGBASENAME-private
  'rgbasename' -> rg-rgbasename-private

단 하위 리소스(VNet, NSG, Foundry 등)의 이름에는 소문자로 변환해 쓴다.
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
Private VNet 주소 공간.
다른 스택과 겹치지 않아야 한다 — public 10.10.0.0/16, private-whitelist 10.30.0.0/16.
''')
param privateVnetAddressPrefix string = '10.20.0.0/16'

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

var STACK_SUFFIX = 'private'

var namePrefix = toLower(resourceGroupBaseName)
var resourceToken = toLower(uniqueString(subscription().id, resourceGroupBaseName, location, STACK_SUFFIX))

var defaultTags = union(tags, {
  'azd-env-name': resourceGroupBaseName
  workload: 'ai-foundry-hol'
  stack: STACK_SUFFIX
})

// 케이스를 보존하기 위해 namePrefix(소문자)가 아니라 원본 값을 쓴다.
var resourceGroupName = 'rg-${resourceGroupBaseName}-${STACK_SUFFIX}'

var bastionSubnetPrefix = cidrSubnet(privateVnetAddressPrefix, 26, 0)
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
    name: 'log-${namePrefix}-${STACK_SUFFIX}-${resourceToken}'
    location: location
    tags: defaultTags
  }
}

var logAnalyticsWorkspaceId = deployLogAnalytics ? logAnalytics!.outputs.id : existingLogAnalyticsWorkspaceId

// 방화벽이 없으므로 플랫폼 서브넷도, UDR도 넘기지 않는다.
// 이 두 매개변수를 비운 것이 private-whitelist 스택과의 유일한 차이다.
module workload '../modules/workload/private-foundry-workload.bicep' = {
  scope: privateResourceGroup
  name: 'private-workload'
  params: {
    namePrefix: namePrefix
    nameSuffix: STACK_SUFFIX
    resourceToken: resourceToken
    location: location
    tags: defaultTags
    vnetAddressPrefix: privateVnetAddressPrefix
    platformSubnets: []
    bastionSubnetPrefix: bastionSubnetPrefix
    privateEndpointSubnetPrefix: privateEndpointSubnetPrefix
    jumpboxSubnetPrefix: jumpboxSubnetPrefix
    jumpboxRouteTableId: ''
    bastionSkuName: bastionSkuName
    privateEndpointNetworkPolicies: privateEndpointNetworkPolicies
    vmSize: vmSize
    vmAdminUsername: vmAdminUsername
    vmAdminPassword: vmAdminPassword
    labUserPrincipalId: labUserPrincipalId
    labUserPrincipalType: labUserPrincipalType
    projectName: 'proj-private'
    projectDisplayName: 'Private 망 실습 프로젝트'
    projectDescription: 'Private Endpoint 경유로만 접근 가능한 Foundry 프로젝트'
    modelDeployments: modelDeployments
    logAnalyticsWorkspaceId: logAnalyticsWorkspaceId
  }
}

module subnetNsgPolicy '../modules/governance/subnet-nsg-policy.bicep' = if (subnetNsgPolicyEffect != 'Disabled') {
  name: 'private-subnet-nsg-policy'
  params: {
    nameSuffix: '${namePrefix}-${STACK_SUFFIX}'
    targetResourceGroupName: resourceGroupName
    effect: subnetNsgPolicyEffect
  }
  dependsOn: [
    workload
  ]
}

// ---------------------------------------------------------------------------
// 출력
// ---------------------------------------------------------------------------

@description('배포 리전')
output AZURE_LOCATION string = location

@description('이 스택의 리소스 그룹')
output PRIVATE_RESOURCE_GROUP string = resourceGroupName

@description('Private VNet 이름')
output PRIVATE_VNET_NAME string = workload.outputs.vnetName

@description('Private Foundry 계정 이름')
output PRIVATE_FOUNDRY_ACCOUNT string = workload.outputs.foundryAccountName

@description('Private Foundry 엔드포인트. 점프박스에서만 해석/호출된다.')
output PRIVATE_FOUNDRY_ENDPOINT string = workload.outputs.foundryEndpoint

@description('Private Foundry 프로젝트 이름')
output PRIVATE_FOUNDRY_PROJECT string = workload.outputs.foundryProjectName

@description('배포된 모델 배포 이름 목록')
output MODEL_DEPLOYMENT_NAMES array = map(modelDeployments, deployment => deployment.name)

@description('Bastion으로 접속할 점프박스 VM 이름')
output JUMPBOX_NAME string = workload.outputs.jumpboxName

@description('점프박스 사설 IP')
output JUMPBOX_PRIVATE_IP string = workload.outputs.jumpboxPrivateIpAddress

@description('Azure Bastion 이름')
output BASTION_NAME string = workload.outputs.bastionName

@description('NSG가 연결되지 않은 서브넷 목록. 이 스택에는 플랫폼 예외가 없으므로 비어 있어야 한다.')
output SUBNETS_WITHOUT_NSG array = workload.outputs.subnetsWithoutNsg
