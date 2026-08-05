metadata description = '''
Foundry 계정 범위의 RBAC 역할 할당 모듈. Keyless 인증의 핵심 부분이다.

disableLocalAuth=true 이므로 API 키가 존재하지 않는다. 실습자와 점프박스 VM의 관리 ID는
여기서 부여한 역할로만 Foundry에 접근할 수 있다.
'''

@export()
@description('역할 할당 정의')
type roleAssignmentConfig = {
  @description('대상 Entra 오브젝트 ID')
  principalId: string

  @description('보안 주체 유형. ARM 복제 지연으로 인한 실패를 막으려면 정확히 지정해야 한다.')
  principalType: ('User' | 'Group' | 'ServicePrincipal')

  @description('역할 정의 GUID')
  roleDefinitionId: string

  @description('할당 목적 설명')
  description: string?
}

@description('대상 Foundry 계정 이름')
param accountName string

@description('역할 할당 목록. principalId가 빈 항목은 호출 측에서 걸러서 넘긴다.')
param assignments roleAssignmentConfig[]

resource account 'Microsoft.CognitiveServices/accounts@2026-05-01' existing = {
  name: accountName
}

resource roleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for assignment in assignments: {
    name: guid(account.id, assignment.principalId, assignment.roleDefinitionId)
    scope: account
    properties: {
      roleDefinitionId: subscriptionResourceId(
        'Microsoft.Authorization/roleDefinitions',
        assignment.roleDefinitionId
      )
      principalId: assignment.principalId
      principalType: assignment.principalType
      description: assignment.?description ?? ''
    }
  }
]

@description('생성된 역할 할당 개수')
output assignmentCount int = length(assignments)
