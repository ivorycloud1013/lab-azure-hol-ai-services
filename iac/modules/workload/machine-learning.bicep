metadata description = '''
Private 망 Azure Machine Learning 묶음 (리소스 그룹 범위에 배포되는 모듈).

만드는 리소스
  - Storage Account (AML 기본 데이터스토어) + blob / file Private Endpoint
  - Key Vault (AML 비밀 저장소) + vault Private Endpoint
  - Azure Machine Learning 워크스페이스 + amlworkspace Private Endpoint
  - 위 Private Endpoint 의 이름 해석에 필요한 Private DNS Zone 5개
  - 실습자와 점프박스 관리 ID 에 대한 AzureML Data Scientist 역할 할당

세 리소스 모두 인터넷에서는 도달할 수 없다.
전용 서브넷(snet-aml)에 놓인 Private Endpoint 가 유일한 데이터 경로이며,
그 서브넷의 NSG 가 점프박스 서브넷에서 오는 443 만 허용하므로 점프박스에서만 도달할 수 있다.

잠그는 방식은 리소스마다 다르다.
  워크스페이스        : publicNetworkAccess=Disabled (공용 엔드포인트 자체를 없앤다)
  스토리지 · Key Vault : networkAcls.defaultAction=Deny + bypass=AzureServices
                        (허용 목록이 비어 있어 인터넷에서는 아무도 못 들어오지만,
                         AML 리소스 공급자는 워크스페이스를 만들기 위해 통과해야 한다.
                         여기까지 Disabled 로 내리면 워크스페이스 생성 자체가 실패한다.)

만들지 않는 것
  - Container Registry: 커스텀 환경(사용자 지정 Docker 이미지)을 빌드할 때만 필요하다.
    실습은 큐레이팅된 환경으로 충분하므로 만들지 않는다. 커스텀 이미지가 필요해지면
    ACR 과 Private Endpoint 를 추가하고 워크스페이스에 containerRegistry 로 연결한다.
  - Application Insights: 워크스페이스의 선택 항목이라 연결하지 않는다.
    필요해지면 ai/machine-learning-workspace.bicep 의 applicationInsightsId 로 넘기면 된다.

컴퓨팅(Compute Instance / Cluster)은 이 모듈이 만들지 않는다.
워크스페이스의 관리형 네트워크를 켜 두었으므로, 실습 중에 컴퓨팅을 만들면 Azure 가 관리하는
별도 VNet 안에 생성되고 데이터스토어에는 관리형 Private Endpoint 로 접근한다.
컴퓨팅용 서브넷을 VNet 에 직접 주입하는 예전 방식보다 이 방식이 권장된다.
'''

import { roleAssignmentConfig } from '../identity/foundry-role-assignments.bicep'
import { AZURE_ML_DATA_SCIENTIST_ROLE_ID } from '../identity/role-definitions.bicep'

@description('리소스 이름 접두사. 소문자여야 한다.')
param namePrefix string

@description('리소스 이름 접미사. 시스템을 구분한다. 예: private')
param nameSuffix string

@description('전역 고유 이름에 사용할 토큰')
param resourceToken string

@description('배포 리전')
param location string

@description('리소스 태그')
param tags object = {}

@description('Private DNS Zone 을 연결할 VNet 리소스 ID')
param virtualNetworkId string

@description('Private Endpoint 를 배치할 서브넷 리소스 ID (snet-aml)')
param subnetId string

@description('Private DNS Zone 을 VNet 에 연결할지 여부. false 면 점프박스가 사설 IP로 해석하지 못한다.')
param linkPrivateDnsZonesToVnet bool = true

@description('워크스페이스 관리형 네트워크 격리 모드')
@allowed(['Disabled', 'AllowInternetOutbound', 'AllowOnlyApprovedOutbound'])
param managedNetworkIsolationMode string = 'AllowInternetOutbound'

@description('워크스페이스 표시 이름')
param workspaceFriendlyName string

@description('워크스페이스 설명')
param workspaceDescription string = ''

@description('권한을 부여할 실습자 Entra 오브젝트 ID. 빈 값이면 실습자 역할 할당을 건너뛴다.')
param labUserPrincipalId string = ''

@description('실습자 보안 주체 유형')
@allowed(['User', 'Group', 'ServicePrincipal'])
param labUserPrincipalType string = 'User'

@description('점프박스 관리 ID 의 principal ID. 빈 값이면 점프박스 역할 할당을 건너뛴다.')
param jumpboxPrincipalId string = ''

@description('진단 로그를 보낼 Log Analytics 작업 영역 ID')
param logAnalyticsWorkspaceId string = ''

// ---------------------------------------------------------------------------
// 이름
//
// 스토리지 계정과 Key Vault 는 전역 고유해야 하고 글자 수 제한도 빡빡하다.
// resourceToken 만으로도 구독·리소스그룹·리전·시스템 조합마다 값이 달라지므로,
// 사람이 읽을 접두사는 짧게 두고 고유성은 토큰에 맡긴다.
// ---------------------------------------------------------------------------

// 3~24자, 소문자와 숫자만. 'stml'(4) + 토큰(13) = 17자
var storageAccountName = 'stml${resourceToken}'

// 3~24자, 영문/숫자/하이픈. 'kv-ml-'(6) + 토큰(13) = 19자
var keyVaultName = 'kv-ml-${resourceToken}'

// 3~33자. 'mlw-'(4) + 접두사(최대 15) + '-'(1) + 토큰(13) = 최대 33자
var workspaceName = 'mlw-${take(namePrefix, 15)}-${resourceToken}'

// ---------------------------------------------------------------------------
// Private DNS Zone
//
// 이름 해석 대상이 서로 다르므로 존을 따로 만든다. 하나라도 빠지면 해당 리소스가
// 공용 IP 로 해석되어, 공용 엔드포인트가 닫힌 상태에서는 접근 자체가 실패한다.
// ---------------------------------------------------------------------------

module dnsZoneWorkspaceApi '../network/private-dns-zone.bicep' = {
  name: 'dns-aml-api-${nameSuffix}'
  params: {
    name: 'privatelink.api.azureml.ms'
    tags: tags
    virtualNetworkId: virtualNetworkId
    linkToVirtualNetwork: linkPrivateDnsZonesToVnet
  }
}

module dnsZoneWorkspaceNotebooks '../network/private-dns-zone.bicep' = {
  name: 'dns-aml-notebooks-${nameSuffix}'
  params: {
    name: 'privatelink.notebooks.azure.net'
    tags: tags
    virtualNetworkId: virtualNetworkId
    linkToVirtualNetwork: linkPrivateDnsZonesToVnet
  }
}

module dnsZoneBlob '../network/private-dns-zone.bicep' = {
  name: 'dns-aml-blob-${nameSuffix}'
  params: {
    name: 'privatelink.blob.${environment().suffixes.storage}'
    tags: tags
    virtualNetworkId: virtualNetworkId
    linkToVirtualNetwork: linkPrivateDnsZonesToVnet
  }
}

module dnsZoneFile '../network/private-dns-zone.bicep' = {
  name: 'dns-aml-file-${nameSuffix}'
  params: {
    name: 'privatelink.file.${environment().suffixes.storage}'
    tags: tags
    virtualNetworkId: virtualNetworkId
    linkToVirtualNetwork: linkPrivateDnsZonesToVnet
  }
}

module dnsZoneKeyVault '../network/private-dns-zone.bicep' = {
  name: 'dns-aml-vault-${nameSuffix}'
  params: {
    name: 'privatelink.vaultcore.azure.net'
    tags: tags
    virtualNetworkId: virtualNetworkId
    linkToVirtualNetwork: linkPrivateDnsZonesToVnet
  }
}

// ---------------------------------------------------------------------------
// 연결 리소스 (스토리지 · Key Vault)
// ---------------------------------------------------------------------------

// 네트워크 설정은 모듈 기본값을 그대로 쓴다.
// defaultAction=Deny + bypass=AzureServices 조합이라 실제 데이터 경로는 Private Endpoint 뿐이고,
// AML 리소스 공급자만 워크스페이스 프로비저닝을 위해 통과한다. 자세한 이유는 모듈 설명에 있다.
module storage '../storage/storage-account.bicep' = {
  name: 'aml-storage-${nameSuffix}'
  params: {
    name: storageAccountName
    location: location
    tags: tags
  }
}

module storageBlobPrivateEndpoint '../network/private-endpoint.bicep' = {
  name: 'aml-storage-blob-pe-${nameSuffix}'
  params: {
    name: 'pe-${storageAccountName}-blob'
    location: location
    tags: tags
    subnetId: subnetId
    targetResourceId: storage.outputs.id
    groupIds: ['blob']
    privateDnsZoneIds: [dnsZoneBlob.outputs.id]
  }
}

// 노트북 파일 공유(코드·프로필)가 file 엔드포인트를 쓴다. blob 만 뚫으면 스튜디오 노트북이 실패한다.
module storageFilePrivateEndpoint '../network/private-endpoint.bicep' = {
  name: 'aml-storage-file-pe-${nameSuffix}'
  params: {
    name: 'pe-${storageAccountName}-file'
    location: location
    tags: tags
    subnetId: subnetId
    targetResourceId: storage.outputs.id
    groupIds: ['file']
    privateDnsZoneIds: [dnsZoneFile.outputs.id]
  }
}

module keyVault '../security/key-vault.bicep' = {
  name: 'aml-keyvault-${nameSuffix}'
  params: {
    name: keyVaultName
    location: location
    tags: tags
  }
}

module keyVaultPrivateEndpoint '../network/private-endpoint.bicep' = {
  name: 'aml-keyvault-pe-${nameSuffix}'
  params: {
    name: 'pe-${keyVaultName}'
    location: location
    tags: tags
    subnetId: subnetId
    targetResourceId: keyVault.outputs.id
    groupIds: ['vault']
    privateDnsZoneIds: [dnsZoneKeyVault.outputs.id]
  }
}

// ---------------------------------------------------------------------------
// 워크스페이스
// ---------------------------------------------------------------------------

module workspace '../ai/machine-learning-workspace.bicep' = {
  name: 'aml-workspace-${nameSuffix}'
  params: {
    name: workspaceName
    location: location
    tags: tags
    friendlyName: workspaceFriendlyName
    workspaceDescription: workspaceDescription
    storageAccountId: storage.outputs.id
    keyVaultId: keyVault.outputs.id
    publicNetworkAccess: 'Disabled'
    managedNetworkIsolationMode: managedNetworkIsolationMode
    logAnalyticsWorkspaceId: logAnalyticsWorkspaceId
  }
  // 연결 리소스의 Private Endpoint 가 먼저 서야 워크스페이스 프로비저닝이 스토리지·Key Vault 에 닿는다.
  dependsOn: [
    storageBlobPrivateEndpoint
    storageFilePrivateEndpoint
    keyVaultPrivateEndpoint
  ]
}

// amlworkspace 그룹 하나로 제어 평면(api)과 노트북(notebooks) 이름이 모두 등록된다.
module workspacePrivateEndpoint '../network/private-endpoint.bicep' = {
  name: 'aml-workspace-pe-${nameSuffix}'
  params: {
    name: 'pe-${workspaceName}'
    location: location
    tags: tags
    subnetId: subnetId
    targetResourceId: workspace.outputs.id
    groupIds: ['amlworkspace']
    privateDnsZoneIds: [
      dnsZoneWorkspaceApi.outputs.id
      dnsZoneWorkspaceNotebooks.outputs.id
    ]
  }
}

// ---------------------------------------------------------------------------
// RBAC
// ---------------------------------------------------------------------------

var labUserAssignments roleAssignmentConfig[] = empty(labUserPrincipalId) ? [] : [
  {
    principalId: labUserPrincipalId
    principalType: labUserPrincipalType
    roleDefinitionId: AZURE_ML_DATA_SCIENTIST_ROLE_ID
    description: '실습자 - AML 워크스페이스에서 실험/작업 수행'
  }
]

var jumpboxAssignments roleAssignmentConfig[] = empty(jumpboxPrincipalId) ? [] : [
  {
    principalId: jumpboxPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: AZURE_ML_DATA_SCIENTIST_ROLE_ID
    description: '점프박스 관리 ID - AML 워크스페이스 호출 (keyless)'
  }
]

module roleAssignments '../identity/machine-learning-role-assignments.bicep' = {
  name: 'aml-roles-${nameSuffix}'
  params: {
    workspaceName: workspace.outputs.name
    assignments: concat(labUserAssignments, jumpboxAssignments)
  }
}

// ---------------------------------------------------------------------------
// 출력
// ---------------------------------------------------------------------------

@description('AML 워크스페이스 리소스 ID')
output workspaceId string = workspace.outputs.id

@description('AML 워크스페이스 이름')
output workspaceName string = workspace.outputs.name

@description('AML 워크스페이스 Private Endpoint 이름')
output workspacePrivateEndpointName string = workspacePrivateEndpoint.outputs.name

@description('AML 기본 데이터스토어 스토리지 계정 이름')
output storageAccountName string = storage.outputs.name

@description('AML Key Vault 이름')
output keyVaultName string = keyVault.outputs.name
