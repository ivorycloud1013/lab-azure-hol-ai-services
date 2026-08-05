metadata description = '''
[스택 1/3] Public 망 — 독립 배포 단위.

전용 리소스 그룹 rg-<기본이름>-public 하나만 만들고 그 안에서 끝난다.
다른 스택(private, private-whitelist)과 어떤 리소스도 공유하지 않으며, 배포 순서 제약도 없다.

접근 경로: 실습자 노트북 -> 인터넷 -> Foundry 공용 엔드포인트

  publicNetworkAccess = Enabled 이지만 networkAcls.defaultAction = Deny 이므로
  "인터넷에 열려 있음"이 아니라 "화이트리스트에 등록된 출발지만 허용"이다.
  인증은 keyless(API 키 비활성 + Entra ID 토큰 + RBAC).
'''

targetScope = 'subscription'

import { modelDeploymentConfig } from '../modules/ai/model-deployments.bicep'

@description('''
리소스 그룹 기본 이름. 최종 이름은 rg-<이 값>-public 이 된다.

케이스는 준 그대로 리소스 그룹 이름에 보존된다.
  'RGBASENAME' -> rg-RGBASENAME-public
  'rgbasename' -> rg-rgbasename-public

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

@description('실습자 노트북의 공인 IP. IP 화이트리스트에 등록된다. curl ifconfig.me 로 확인한다.')
param labClientIpAddress string

@description('keyless 접근 권한을 부여할 실습자 Entra 오브젝트 ID. 빈 값이면 역할 할당을 건너뛴다.')
param labUserPrincipalId string = ''

@description('실습자 보안 주체 유형')
@allowed(['User', 'Group', 'ServicePrincipal'])
param labUserPrincipalType string = 'User'

@description('Public VNet 주소 공간. private(10.20.0.0/16) · private-whitelist(10.30.0.0/16)와 겹치지 않아야 한다.')
param vnetAddressPrefix string = '10.10.0.0/16'

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

var namePrefix = toLower(resourceGroupBaseName)
var resourceToken = toLower(uniqueString(subscription().id, resourceGroupBaseName, location))

var defaultTags = union(tags, {
  'azd-env-name': resourceGroupBaseName
  workload: 'ai-foundry-hol'
  stack: 'public'
})

// 케이스를 보존하기 위해 namePrefix(소문자)가 아니라 원본 값을 쓴다.
var resourceGroupName = 'rg-${resourceGroupBaseName}-public'

resource publicResourceGroup 'Microsoft.Resources/resourceGroups@2025-04-01' = {
  name: resourceGroupName
  location: location
  tags: defaultTags
}

module logAnalytics '../modules/monitor/log-analytics.bicep' = if (deployLogAnalytics) {
  scope: publicResourceGroup
  name: 'public-log-analytics'
  params: {
    name: 'log-${namePrefix}-public-${resourceToken}'
    location: location
    tags: defaultTags
  }
}

var logAnalyticsWorkspaceId = deployLogAnalytics ? logAnalytics!.outputs.id : existingLogAnalyticsWorkspaceId

module resources './resources.bicep' = {
  scope: publicResourceGroup
  name: 'public-resources'
  params: {
    namePrefix: namePrefix
    resourceToken: resourceToken
    location: location
    tags: defaultTags
    vnetAddressPrefix: vnetAddressPrefix
    workloadSubnetPrefix: cidrSubnet(vnetAddressPrefix, 24, 1)
    labClientIpAddress: labClientIpAddress
    labUserPrincipalId: labUserPrincipalId
    labUserPrincipalType: labUserPrincipalType
    modelDeployments: modelDeployments
    logAnalyticsWorkspaceId: logAnalyticsWorkspaceId
  }
}

@description('배포 리전')
output AZURE_LOCATION string = location

@description('Public 망 리소스 그룹')
output PUBLIC_RESOURCE_GROUP string = resourceGroupName

@description('Public Foundry 계정 이름')
output PUBLIC_FOUNDRY_ACCOUNT string = resources.outputs.foundryAccountName

@description('Public Foundry 엔드포인트. 노트북에서 직접 호출한다.')
output PUBLIC_FOUNDRY_ENDPOINT string = resources.outputs.foundryEndpoint

@description('Public Foundry 프로젝트 이름')
output PUBLIC_FOUNDRY_PROJECT string = resources.outputs.foundryProjectName

@description('배포된 모델 배포 이름 목록')
output MODEL_DEPLOYMENT_NAMES array = map(modelDeployments, deployment => deployment.name)

@description('IP 화이트리스트에 등록된 실습자 IP')
output ALLOWED_CLIENT_IP string = labClientIpAddress

@description('NSG가 없는 서브넷 목록. 비어 있어야 정상이다.')
output SUBNETS_WITHOUT_NSG array = resources.outputs.subnetsWithoutNsg
