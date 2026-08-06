metadata description = '''
Application Insights 모듈.

Azure Machine Learning 워크스페이스의 필수 의존 리소스다.
storageAccount · keyVault · applicationInsights 세 가지가 모두 있어야 워크스페이스를 만들 수 있고,
하나라도 비면 "Missing dependent resources in workspace json" 으로 배포가 실패한다.

클래식(독립형) Application Insights 는 사용이 중단되어, 반드시 Log Analytics 작업 영역에
연결된 workspace-based 로 만들어야 한다. 그래서 workspaceResourceId 가 필수 매개변수다.
'''

@description('Application Insights 이름')
param name string

@description('배포 리전')
param location string

@description('리소스 태그')
param tags object = {}

@description('로그를 저장할 Log Analytics 작업 영역 리소스 ID. workspace-based 구성에 반드시 필요하다.')
param workspaceResourceId string

resource applicationInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: name
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: workspaceResourceId
    // Bluefield 는 workspace-based 리소스를 만들 때 쓰는 값이다.
    Flow_Type: 'Bluefield'
    // 수집 경로는 열어 두고 조회는 Azure Portal / API 로만 한다.
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

@description('Application Insights 리소스 ID')
output id string = applicationInsights.id

@description('Application Insights 이름')
output name string = applicationInsights.name
