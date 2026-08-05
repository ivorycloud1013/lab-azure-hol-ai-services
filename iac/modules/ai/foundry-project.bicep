metadata description = '''
Azure AI Foundry 프로젝트 모듈 (Microsoft.CognitiveServices/accounts/projects).

프로젝트에도 SystemAssigned 관리 ID를 부여해서, 프로젝트 단위 연결(connection)에서도
키 대신 Entra ID 토큰을 쓰도록 한다.
'''

@description('상위 Foundry 계정 이름')
param accountName string

@description('프로젝트 이름')
@minLength(2)
@maxLength(64)
param name string

@description('배포 리전')
param location string

@description('리소스 태그')
param tags object = {}

@description('Foundry 포털에 표시될 이름')
param displayName string

@description('프로젝트 설명')
param projectDescription string = ''

resource account 'Microsoft.CognitiveServices/accounts@2026-05-01' existing = {
  name: accountName
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2026-05-01' = {
  parent: account
  name: name
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    displayName: displayName
    description: projectDescription
  }
}

@description('프로젝트 리소스 ID')
output id string = project.id

@description('프로젝트 이름')
output name string = project.name

@description('프로젝트 시스템 할당 관리 ID의 principal ID')
output principalId string = project.identity.principalId
