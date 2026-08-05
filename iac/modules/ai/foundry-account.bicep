metadata description = '''
Azure AI Foundry 계정 모듈 (Microsoft.CognitiveServices/accounts, kind=AIServices).

Keyless 인증:
  - disableLocalAuth=true 로 API 키 인증을 완전히 끈다. 이후 접근은 Entra ID 토큰 + RBAC만 가능하다.
  - 토큰 기반 인증에는 customSubDomainName이 반드시 필요하다(전역 고유해야 함).
  - 계정에 SystemAssigned 관리 ID를 부여해 하위 리소스 연동 시에도 키를 쓰지 않는다.

네트워크:
  - networkAcls.defaultAction은 항상 Deny. 허용 대상만 ipRules/virtualNetworkRules로 등록한다.
  - bypass=\'None\'이면 "신뢰할 수 있는 Azure 서비스"조차 우회하지 못한다.
    Private 망 요구사항("Azure도 막고 화이트리스트로만 뚫는다")에 맞추려면 None을 쓴다.
'''

@description('Foundry 계정 이름')
@minLength(2)
@maxLength(64)
param name string

@description('배포 리전')
param location string

@description('리소스 태그')
param tags object = {}

@description('전역 고유해야 하는 커스텀 서브도메인. keyless(Entra ID) 인증에 필수다.')
param customSubDomainName string

@description('공용 네트워크 접근 허용 여부. Private 망 계정은 Disabled.')
@allowed(['Enabled', 'Disabled'])
param publicNetworkAccess string = 'Disabled'

@description('API 키(로컬 인증)를 비활성화할지 여부. keyless 실습에서는 항상 true.')
param disableLocalAuth bool = true

@description('허용할 클라이언트 공인 IP 또는 CIDR 목록. publicNetworkAccess=Enabled일 때만 의미가 있다.')
param allowedIpAddresses string[] = []

@description('허용할 서브넷 리소스 ID 목록. 해당 서브넷에 Microsoft.CognitiveServices 서비스 엔드포인트가 필요하다.')
param allowedSubnetIds string[] = []

@description('네트워크 ACL 우회 범위. None이면 신뢰할 수 있는 Azure 서비스도 우회하지 못한다.')
@allowed(['None', 'AzureServices'])
param networkAclsBypass string = 'None'

@description('진단 로그를 보낼 Log Analytics 작업 영역 ID. 빈 문자열이면 진단 설정을 만들지 않는다.')
param logAnalyticsWorkspaceId string = ''

resource account 'Microsoft.CognitiveServices/accounts@2026-05-01' = {
  name: name
  location: location
  tags: tags
  kind: 'AIServices'
  sku: {
    name: 'S0'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    // Foundry 프로젝트를 이 계정 하위에 만들 수 있게 한다.
    allowProjectManagement: true
    customSubDomainName: customSubDomainName
    disableLocalAuth: disableLocalAuth
    publicNetworkAccess: publicNetworkAccess
    networkAcls: {
      defaultAction: 'Deny'
      bypass: networkAclsBypass
      ipRules: [
        for ipAddress in allowedIpAddresses: {
          value: ipAddress
        }
      ]
      virtualNetworkRules: [
        for subnetId in allowedSubnetIds: {
          id: subnetId
          ignoreMissingVnetServiceEndpoint: false
        }
      ]
    }
  }
}

resource diagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = if (!empty(logAnalyticsWorkspaceId)) {
  name: 'diag-to-law'
  scope: account
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

@description('Foundry 계정 리소스 ID')
output id string = account.id

@description('Foundry 계정 이름')
output name string = account.name

@description('Foundry 계정 엔드포인트')
output endpoint string = account.properties.endpoint

@description('계정 시스템 할당 관리 ID의 principal ID')
output principalId string = account.identity.principalId
