metadata description = '''
Azure Machine Learning 워크스페이스 모듈 (Microsoft.MachineLearningServices/workspaces, kind=Default).

필수 의존 리소스
  storageAccount · keyVault · applicationInsights 세 가지를 모두 넘겨야 한다.
  하나라도 비면 리소스 공급자가 "Missing dependent resources in workspace json" 으로 거부한다.
  containerRegistry 는 커스텀 Docker 이미지를 빌드할 때만 필요한 선택 항목이다.

네트워크
  - publicNetworkAccess=Disabled 로 공용 엔드포인트를 닫는다.
    이후 제어 평면(스튜디오·SDK·CLI) 접근은 amlworkspace Private Endpoint 로만 가능하다.
  - managedNetwork.isolationMode 로 "관리형 네트워크"를 켠다.
    컴퓨팅 인스턴스와 클러스터는 Azure 가 관리하는 별도 VNet 안에 만들어지고,
    스토리지·Key Vault 같은 연결 리소스에는 관리형 Private Endpoint 로 접근한다.
    실습자가 서브넷을 직접 설계하지 않아도 되므로 현재 권장되는 방식이다.

    AllowInternetOutbound      : 아웃바운드 인터넷 허용 (pip install 등이 그대로 동작)
    AllowOnlyApprovedOutbound  : 승인한 대상으로만 아웃바운드 허용 (가장 엄격)
    Disabled                   : 관리형 네트워크를 쓰지 않음 (컴퓨팅을 만들 수 없거나 공용 경로를 탄다)

  관리형 네트워크는 워크스페이스를 만들 때가 아니라 첫 컴퓨팅을 만들 때 프로비저닝된다.
  그래서 이 템플릿의 배포 시간에는 영향을 주지 않는다.

인증
  워크스페이스에 SystemAssigned 관리 ID 를 부여한다. 이 ID 에 대한 스토리지·Key Vault
  역할 할당은 Azure Machine Learning 리소스 공급자가 자동으로 만들어 주므로 여기서 다루지 않는다.
'''

@description('워크스페이스 이름. 3~33자 영문/숫자/하이픈만 쓸 수 있다.')
@minLength(3)
@maxLength(33)
param name string

@description('배포 리전')
param location string

@description('리소스 태그')
param tags object = {}

@description('스튜디오에 표시할 이름')
param friendlyName string

@description('워크스페이스 설명')
param workspaceDescription string = ''

@description('기본 데이터스토어로 연결할 스토리지 계정 리소스 ID')
param storageAccountId string

@description('비밀 저장소로 연결할 Key Vault 리소스 ID')
param keyVaultId string

@description('''
연결할 Application Insights 리소스 ID. 선택 항목이 아니라 필수다.

kind=Default 워크스페이스는 storageAccount · keyVault · applicationInsights 세 가지를 모두
요구한다. 하나라도 비우면 리소스 공급자가 다음 오류로 거부한다.
  ValidationError: Missing dependent resources in workspace json
기본값을 두지 않는 이유는, 빈 값이 null 로 나가 조용히 실패하는 경로를 막기 위해서다.
''')
@minLength(1)
param applicationInsightsId string

@description('공용 네트워크 접근 허용 여부. Private 망 워크스페이스는 Disabled.')
@allowed(['Enabled', 'Disabled'])
param publicNetworkAccess string = 'Disabled'

@description('관리형 네트워크 격리 모드. 컴퓨팅이 놓일 네트워크를 결정한다.')
@allowed(['Disabled', 'AllowInternetOutbound', 'AllowOnlyApprovedOutbound'])
param managedNetworkIsolationMode string = 'AllowInternetOutbound'

@description('진단 로그를 보낼 Log Analytics 작업 영역 ID. 빈 문자열이면 진단 설정을 만들지 않는다.')
param logAnalyticsWorkspaceId string = ''

resource workspace 'Microsoft.MachineLearningServices/workspaces@2024-10-01' = {
  name: name
  location: location
  tags: tags
  kind: 'Default'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    friendlyName: friendlyName
    description: workspaceDescription
    storageAccount: storageAccountId
    keyVault: keyVaultId
    applicationInsights: applicationInsightsId
    publicNetworkAccess: publicNetworkAccess
    // v1 호환 모드를 끄면 v2 API 와 RBAC 기반 권한 모델을 그대로 쓴다.
    v1LegacyMode: false
    hbiWorkspace: false
    managedNetwork: {
      isolationMode: managedNetworkIsolationMode
    }
  }
}

resource diagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = if (!empty(logAnalyticsWorkspaceId)) {
  name: 'diag-to-law'
  scope: workspace
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [
      {
        categoryGroup: 'allLogs'
        enabled: true
      }
    ]
    metrics: [
      {
        category: 'AllMetrics'
        enabled: true
      }
    ]
  }
}

@description('워크스페이스 리소스 ID')
output id string = workspace.id

@description('워크스페이스 이름')
output name string = workspace.name

@description('워크스페이스 시스템 할당 관리 ID의 principal ID')
output principalId string = workspace.identity.principalId
