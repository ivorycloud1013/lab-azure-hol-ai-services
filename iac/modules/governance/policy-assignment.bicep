metadata description = '''
리소스 그룹 범위 정책 할당 모듈.

정책 정의는 구독 범위, 할당은 리소스 그룹 범위라 Bicep에서 서로 다른 스코프가 된다.
Bicep은 파일의 targetScope와 다른 스코프의 리소스를 직접 선언할 수 없으므로(BCP139)
할당만 이 모듈로 분리한다.
'''

targetScope = 'resourceGroup'

@description('정책 할당 이름')
param name string

@description('표시 이름')
param displayName string

@description('할당할 정책 정의 리소스 ID')
param policyDefinitionId string

@description('정책에 전달할 매개변수')
param policyParameters object = {}

@description('정책 시행 모드. DoNotEnforce면 평가만 하고 효과를 적용하지 않는다.')
@allowed(['Default', 'DoNotEnforce'])
param enforcementMode string = 'Default'

resource policyAssignment 'Microsoft.Authorization/policyAssignments@2024-04-01' = {
  name: name
  properties: {
    displayName: displayName
    policyDefinitionId: policyDefinitionId
    enforcementMode: enforcementMode
    parameters: policyParameters
  }
}

@description('정책 할당 리소스 ID')
output id string = policyAssignment.id
