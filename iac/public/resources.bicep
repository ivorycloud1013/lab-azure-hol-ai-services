metadata description = '''
Public 망 리소스 (리소스 그룹 범위).

통제 방식
  - publicNetworkAccess=Enabled + networkAcls.defaultAction=Deny.
    실습자 노트북 공인 IP를 ipRules에, 워크로드 서브넷을 virtualNetworkRules에 등록한다.
  - 인증은 keyless(disableLocalAuth=true + Entra ID 토큰 + RBAC).
  - VNet에 붙는 모든 서브넷에 NSG를 연결하고 deny-all 기준선을 적용한다.
'''

import { subnetConfig } from '../modules/network/vnet.bicep'
import { modelDeploymentConfig } from '../modules/ai/model-deployments.bicep'
import { roleAssignmentConfig } from '../modules/identity/foundry-role-assignments.bicep'
import {
  COGNITIVE_SERVICES_USER_ROLE_ID
  COGNITIVE_SERVICES_OPENAI_USER_ROLE_ID
  AZURE_AI_DEVELOPER_ROLE_ID
} from '../modules/identity/role-definitions.bicep'

@description('리소스 이름 접두사')
param namePrefix string

@description('전역 고유 이름에 사용할 토큰')
param resourceToken string

@description('배포 리전')
param location string

@description('리소스 태그')
param tags object = {}

@description('Public VNet 주소 공간')
param vnetAddressPrefix string

@description('Public 워크로드 서브넷 CIDR')
param workloadSubnetPrefix string

@description('실습자 노트북 공인 IP. 빈 값이면 IP 화이트리스트 없이 배포되어 아무도 접근할 수 없다.')
param labClientIpAddress string = ''

@description('권한을 부여할 실습자 Entra 오브젝트 ID. 빈 값이면 역할 할당을 건너뛴다.')
param labUserPrincipalId string = ''

@description('실습자 보안 주체 유형')
@allowed(['User', 'Group', 'ServicePrincipal'])
param labUserPrincipalType string = 'User'

@description('Foundry 프로젝트 이름')
param projectName string = 'proj-pub-${location}'

@description('배포할 모델 목록')
param modelDeployments modelDeploymentConfig[]

@description('진단 로그를 보낼 Log Analytics 작업 영역 ID')
param logAnalyticsWorkspaceId string = ''

var workloadSubnetName = 'snet-workload'

var workloadNsgRules = [
  {
    name: 'AllowCognitiveServicesOutbound'
    properties: {
      description: 'Foundry 공용 엔드포인트 호출 허용.'
      priority: 100
      direction: 'Outbound'
      access: 'Allow'
      protocol: 'Tcp'
      sourceAddressPrefix: workloadSubnetPrefix
      sourcePortRange: '*'
      destinationAddressPrefix: 'CognitiveServicesManagement'
      destinationPortRange: '443'
    }
  }
  {
    name: 'AllowEntraIdOutbound'
    properties: {
      description: 'keyless 인증용 Entra ID 토큰 발급 허용.'
      priority: 110
      direction: 'Outbound'
      access: 'Allow'
      protocol: 'Tcp'
      sourceAddressPrefix: workloadSubnetPrefix
      sourcePortRange: '*'
      destinationAddressPrefix: 'AzureActiveDirectory'
      destinationPortRange: '443'
    }
  }
]

module workloadNsg '../modules/network/nsg.bicep' = {
  name: 'public-nsg-workload'
  params: {
    name: 'nsg-${namePrefix}-public-workload'
    location: location
    tags: tags
    securityRules: workloadNsgRules
    enableDenyAllBaseline: true
    logAnalyticsWorkspaceId: logAnalyticsWorkspaceId
  }
}

var subnets subnetConfig[] = [
  {
    name: workloadSubnetName
    addressPrefix: workloadSubnetPrefix
    networkSecurityGroupId: workloadNsg.outputs.id
    // Foundry의 virtualNetworkRules가 동작하려면 이 서비스 엔드포인트가 필요하다.
    serviceEndpoints: ['Microsoft.CognitiveServices']
  }
]

module vnet '../modules/network/vnet.bicep' = {
  name: 'public-vnet'
  params: {
    name: 'vnet-${namePrefix}-public'
    location: location
    tags: tags
    addressPrefixes: [vnetAddressPrefix]
    subnets: subnets
  }
}

// 계정 이름에 리전을 넣어, 같은 리소스 그룹에 여러 리전의 Foundry를 둘 수 있게 한다.
// 최대 길이: aif-(4) + namePrefix(16) + -pub-(5) + location(18) + -(1) + token(13) = 57자
var foundryAccountName = 'aif-${namePrefix}-pub-${location}-${resourceToken}'

module foundry '../modules/ai/foundry-account.bicep' = {
  name: 'public-foundry-account'
  params: {
    name: foundryAccountName
    location: location
    tags: tags
    customSubDomainName: foundryAccountName
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: true
    allowedIpAddresses: empty(labClientIpAddress) ? [] : [labClientIpAddress]
    allowedSubnetIds: [vnet.outputs.subnetIds[workloadSubnetName]]
    networkAclsBypass: 'AzureServices'
    logAnalyticsWorkspaceId: logAnalyticsWorkspaceId
  }
}

module project '../modules/ai/foundry-project.bicep' = {
  name: 'public-foundry-project'
  params: {
    accountName: foundry.outputs.name
    name: projectName
    location: location
    tags: tags
    displayName: 'Public 망 실습 프로젝트'
    projectDescription: '인터넷 경유 + IP 화이트리스트로 접근하는 Foundry 프로젝트'
  }
}

module deployments '../modules/ai/model-deployments.bicep' = {
  name: 'public-foundry-deployments'
  params: {
    accountName: foundry.outputs.name
    deployments: modelDeployments
  }
  dependsOn: [
    project
  ]
}

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
  name: 'public-foundry-roles'
  params: {
    accountName: foundry.outputs.name
    assignments: labUserAssignments
  }
}

@description('Public VNet 리소스 ID')
output vnetId string = vnet.outputs.id

@description('NSG가 없는 서브넷 목록. 비어 있어야 정상이다.')
output subnetsWithoutNsg string[] = vnet.outputs.subnetsWithoutNsg

@description('Public Foundry 계정 이름')
output foundryAccountName string = foundry.outputs.name

@description('Public Foundry 엔드포인트')
output foundryEndpoint string = foundry.outputs.endpoint

@description('Public Foundry 프로젝트 이름')
output foundryProjectName string = project.outputs.name
