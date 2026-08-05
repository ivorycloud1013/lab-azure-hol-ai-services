metadata description = '''
Log Analytics 작업 영역 모듈.

Azure Firewall의 화이트리스트가 실제로 무엇을 막고 무엇을 통과시켰는지 확인하려면
AZFWApplicationRule / AZFWNetworkRule 로그가 필요하다. 이 실습의 핵심 관찰 지점이다.
'''

@description('작업 영역 이름')
param name string

@description('배포 리전')
param location string

@description('리소스 태그')
param tags object = {}

@description('로그 보존 기간(일). 실습 환경이므로 최소값인 30일을 기본으로 한다.')
@minValue(30)
@maxValue(730)
param retentionInDays int = 30

@description('일일 수집 상한(GB). -1이면 무제한. 실습 비용 사고를 막기 위해 기본 1GB로 제한한다.')
param dailyQuotaGb int = 1

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: name
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: retentionInDays
    workspaceCapping: {
      dailyQuotaGb: dailyQuotaGb
    }
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

@description('작업 영역 리소스 ID')
output id string = workspace.id

@description('작업 영역 이름')
output name string = workspace.name

@description('작업 영역 고객 ID')
output customerId string = workspace.properties.customerId
