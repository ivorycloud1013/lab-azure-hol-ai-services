metadata description = '''
추가 리전 Foundry의 리소스 그룹 범위 배포.

기존 시스템이 만든 네트워크를 재사용한다. 여기서 만드는 것은
Foundry 계정 · 프로젝트 · 모델 배포 · 역할 할당, 그리고 private 계열이면 Private Endpoint뿐이다.
'''

import { modelDeploymentConfig } from '../modules/ai/model-deployments.bicep'
import { roleAssignmentConfig } from '../modules/identity/foundry-role-assignments.bicep'
import {
  COGNITIVE_SERVICES_USER_ROLE_ID
  COGNITIVE_SERVICES_OPENAI_USER_ROLE_ID
  AZURE_AI_DEVELOPER_ROLE_ID
} from '../modules/identity/role-definitions.bicep'

@description('리소스 이름 접두사')
param namePrefix string

@description('Foundry 계정 이름에 쓰는 짧은 시스템 약어')
param systemAbbrev string

@description('대상 시스템 이름. 기존 VNet 이름을 찾는 데 쓴다.')
param targetSystem string

@description('전역 고유 이름에 사용할 토큰')
param resourceToken string

@description('추가할 Foundry의 리전')
param location string

@description('리소스 태그')
param tags object = {}

@description('대상이 public 시스템인지 여부. Private Endpoint 생성 여부를 가른다.')
param isPublicSystem bool

@description('public 시스템일 때 허용할 실습자 공인 IP')
param labClientIpAddress string = ''

@description('keyless 접근 권한을 부여할 실습자 Entra 오브젝트 ID')
param labUserPrincipalId string = ''

@description('실습자 보안 주체 유형')
@allowed(['User', 'Group', 'ServicePrincipal'])
param labUserPrincipalType string = 'User'

@description('배포할 모델 목록')
param modelDeployments modelDeploymentConfig[]

@description('진단 로그를 보낼 Log Analytics 작업 영역 ID')
param logAnalyticsWorkspaceId string = ''

// ---------------------------------------------------------------------------
// 기존 시스템이 만들어 둔 네트워크 — 이름 규칙으로 찾는다
// ---------------------------------------------------------------------------

// 세 시스템 모두 vnet-<namePrefix>-<targetSystem> 규칙을 따른다.
var existingVnetName = 'vnet-${namePrefix}-${targetSystem}'

// private 계열의 Private Endpoint 전용 서브넷. public 시스템에는 없다.
var privateEndpointSubnetName = 'snet-private-endpoint'

// 기존 시스템이 만든 Private DNS Zone. private-foundry-workload.bicep의 목록과 같아야 한다.
var privateDnsZoneNames = [
  'privatelink.cognitiveservices.azure.com'
  'privatelink.openai.azure.com'
  'privatelink.services.ai.azure.com'
]

resource existingVnet 'Microsoft.Network/virtualNetworks@2025-07-01' existing = {
  name: existingVnetName
}

// .id는 이름으로 계산되는 값이라, public 시스템처럼 이 서브넷이 없는 경우에도
// 템플릿 컴파일과 배포에는 문제가 없다. 실제 참조는 private 계열에서만 일어난다.
resource existingPrivateEndpointSubnet 'Microsoft.Network/virtualNetworks/subnets@2025-07-01' existing = {
  parent: existingVnet
  name: privateEndpointSubnetName
}

resource existingPrivateDnsZones 'Microsoft.Network/privateDnsZones@2024-06-01' existing = [
  for zoneName in privateDnsZoneNames: {
    name: zoneName
  }
]

// ---------------------------------------------------------------------------
// Foundry
// ---------------------------------------------------------------------------

var foundryAccountName = 'aif-${namePrefix}-${systemAbbrev}-${location}-${resourceToken}'
var foundryProjectName = 'proj-${systemAbbrev}-${location}'

module foundry '../modules/ai/foundry-account.bicep' = {
  name: 'foundry-account-${location}'
  params: {
    name: foundryAccountName
    location: location
    tags: tags
    customSubDomainName: foundryAccountName
    publicNetworkAccess: isPublicSystem ? 'Enabled' : 'Disabled'
    disableLocalAuth: true
    allowedIpAddresses: isPublicSystem && !empty(labClientIpAddress) ? [labClientIpAddress] : []
    // 서비스 엔드포인트 기반 VNet 규칙은 서브넷과 계정이 같은 리전일 때만 동작한다.
    // 추가 리전 Foundry는 정의상 리전이 다르므로 VNet 규칙을 걸지 않는다.
    // public 시스템에서 이 계정의 접근 통제는 IP 화이트리스트 하나뿐이다.
    allowedSubnetIds: []
    networkAclsBypass: isPublicSystem ? 'AzureServices' : 'None'
    logAnalyticsWorkspaceId: logAnalyticsWorkspaceId
  }
}

// Private Endpoint는 VNet과 같은 리전에 만들고, 다른 리전에 있는 계정을 가리킨다.
// Private Endpoint의 리전 간 연결은 Azure가 지원한다.
module foundryPrivateEndpoint '../modules/network/private-endpoint.bicep' = if (!isPublicSystem) {
  name: 'foundry-pe-${location}'
  params: {
    name: 'pe-${foundryAccountName}'
    location: existingVnet.location
    tags: tags
    subnetId: existingPrivateEndpointSubnet.id
    targetResourceId: foundry.outputs.id
    groupIds: ['account']
    privateDnsZoneIds: [
      for (zoneName, index) in privateDnsZoneNames: existingPrivateDnsZones[index].id
    ]
  }
}

module project '../modules/ai/foundry-project.bicep' = {
  name: 'foundry-project-${location}'
  params: {
    accountName: foundry.outputs.name
    name: foundryProjectName
    location: location
    tags: tags
    displayName: '${location} 리전 실습 프로젝트'
    projectDescription: '${targetSystem} 시스템에 추가한 ${location} 리전 Foundry 프로젝트'
  }
}

module deployments '../modules/ai/model-deployments.bicep' = {
  name: 'foundry-deployments-${location}'
  params: {
    accountName: foundry.outputs.name
    deployments: modelDeployments
  }
  dependsOn: [
    project
  ]
}

// ---------------------------------------------------------------------------
// RBAC — API 키를 쓰지 않으므로 역할 할당이 없으면 호출할 수 없다
// ---------------------------------------------------------------------------

var labUserAssignments roleAssignmentConfig[] = empty(labUserPrincipalId) ? [] : [
  {
    principalId: labUserPrincipalId
    principalType: labUserPrincipalType
    roleDefinitionId: COGNITIVE_SERVICES_OPENAI_USER_ROLE_ID
    description: '실습자 - OpenAI 모델 추론 호출 (keyless)'
  }
  {
    principalId: labUserPrincipalId
    principalType: labUserPrincipalType
    roleDefinitionId: COGNITIVE_SERVICES_USER_ROLE_ID
    description: '실습자 - AI Services 데이터 평면 호출 (keyless)'
  }
  {
    principalId: labUserPrincipalId
    principalType: labUserPrincipalType
    roleDefinitionId: AZURE_AI_DEVELOPER_ROLE_ID
    description: '실습자 - Foundry 프로젝트 작업'
  }
]

module roleAssignments '../modules/identity/foundry-role-assignments.bicep' = if (!empty(labUserPrincipalId)) {
  name: 'foundry-roles-${location}'
  params: {
    accountName: foundry.outputs.name
    assignments: labUserAssignments
  }
}

@description('추가된 Foundry 계정 이름')
output foundryAccountName string = foundry.outputs.name

@description('추가된 Foundry 엔드포인트')
output foundryEndpoint string = foundry.outputs.endpoint

@description('추가된 Foundry 프로젝트 이름')
output foundryProjectName string = project.outputs.name
