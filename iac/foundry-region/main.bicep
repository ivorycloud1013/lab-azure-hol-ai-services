metadata description = '''
추가 리전 Foundry.

이미 배포된 public / private / private-whitelist 리소스 그룹에
다른 리전의 Azure AI Foundry 계정과 프로젝트를 덧붙인다.

네트워크는 새로 만들지 않는다. 기존 시스템이 만들어 둔 VNet, 서브넷,
Private DNS Zone을 그대로 재사용하므로 대상 시스템이 먼저 배포돼 있어야 한다.

리소스 그룹은 리전에 묶이지 않으므로, 한 리소스 그룹 안에 여러 리전의
Foundry 계정을 둘 수 있다. 계정 이름에 리전이 들어가 서로 충돌하지 않는다.
'''

targetScope = 'subscription'

import { modelDeploymentConfig } from '../modules/ai/model-deployments.bicep'

@minLength(2)
@maxLength(16)
@description('대상 시스템을 배포할 때 쓴 resourceGroupBaseName과 같은 값이어야 한다.')
param resourceGroupBaseName string

@description('Foundry를 덧붙일 기존 시스템. 리소스 그룹 rg-<base>-<targetSystem>을 찾는다.')
@allowed(['public', 'private', 'private-whitelist'])
param targetSystem string

@description('추가할 Foundry의 리전. 기존 시스템의 리전과 다른 값을 넣는다.')
param location string

@description('모든 리소스에 적용할 추가 태그')
param tags object = {}

@description('''
public 대상일 때 허용할 실습자 공인 IP.

private / private-whitelist 대상에서는 무시된다(Private Endpoint로만 접근한다).
''')
param labClientIpAddress string = ''

@description('keyless 접근 권한을 부여할 실습자 Entra 오브젝트 ID. 빈 값이면 역할 할당을 건너뛴다.')
param labUserPrincipalId string = ''

@description('실습자 보안 주체 유형')
@allowed(['User', 'Group', 'ServicePrincipal'])
param labUserPrincipalType string = 'User'

@description('배포할 모델 목록. 리전마다 제공되는 모델이 다르므로 확인 후 지정한다.')
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

@description('진단 로그를 보낼 Log Analytics 작업 영역 ID. 비우면 진단 설정을 만들지 않는다.')
param logAnalyticsWorkspaceId string = ''

// ---------------------------------------------------------------------------
// 이름 규칙 — 기존 시스템과 같은 규칙을 따라야 리소스를 찾을 수 있다
// ---------------------------------------------------------------------------

var namePrefix = toLower(resourceGroupBaseName)
var resourceGroupName = 'rg-${resourceGroupBaseName}-${targetSystem}'

// Foundry 계정 이름 전용 약어. 각 시스템의 main.bicep에 있는 SYSTEM_ABBREV와 같아야 한다.
var SYSTEM_ABBREV = {
  public: 'pub'
  private: 'priv'
  'private-whitelist': 'privwl'
}
var systemAbbrev = SYSTEM_ABBREV[targetSystem]

// 리전이 다르면 토큰도 달라지므로 기존 계정과 이름이 겹치지 않는다.
var resourceToken = toLower(uniqueString(subscription().id, resourceGroupBaseName, location, targetSystem))

var isPublicSystem = targetSystem == 'public'

var defaultTags = union(tags, {
  'azd-env-name': resourceGroupBaseName
  workload: 'ai-foundry-hol'
  stack: targetSystem
  'foundry-region': location
})

// ---------------------------------------------------------------------------

resource targetResourceGroup 'Microsoft.Resources/resourceGroups@2025-04-01' existing = {
  name: resourceGroupName
}

module foundryRegion './resources.bicep' = {
  scope: targetResourceGroup
  name: 'foundry-region-${location}'
  params: {
    namePrefix: namePrefix
    systemAbbrev: systemAbbrev
    targetSystem: targetSystem
    resourceToken: resourceToken
    location: location
    tags: defaultTags
    isPublicSystem: isPublicSystem
    labClientIpAddress: labClientIpAddress
    labUserPrincipalId: labUserPrincipalId
    labUserPrincipalType: labUserPrincipalType
    modelDeployments: modelDeployments
    logAnalyticsWorkspaceId: logAnalyticsWorkspaceId
  }
}

@description('Foundry가 배포된 리전')
output FOUNDRY_REGION string = location

@description('Foundry를 덧붙인 리소스 그룹')
output TARGET_RESOURCE_GROUP string = resourceGroupName

@description('추가된 Foundry 계정 이름')
output FOUNDRY_ACCOUNT string = foundryRegion.outputs.foundryAccountName

@description('추가된 Foundry 엔드포인트')
output FOUNDRY_ENDPOINT string = foundryRegion.outputs.foundryEndpoint

@description('추가된 Foundry 프로젝트 이름')
output FOUNDRY_PROJECT string = foundryRegion.outputs.foundryProjectName

@description('배포된 모델 배포 이름 목록')
output MODEL_DEPLOYMENT_NAMES array = map(modelDeployments, deployment => deployment.name)

@description('접근 경로. public은 IP 화이트리스트, 나머지는 Private Endpoint다.')
output ACCESS_PATH string = isPublicSystem ? 'IP whitelist' : 'Private Endpoint'
