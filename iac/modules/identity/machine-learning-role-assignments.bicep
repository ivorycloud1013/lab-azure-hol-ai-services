metadata description = '''
Azure Machine Learning 워크스페이스 범위의 RBAC 역할 할당 모듈.

역할 할당 정의 타입(roleAssignmentConfig)은 foundry-role-assignments.bicep 에서 내보낸 것을
그대로 가져다 쓴다. 두 모듈은 대상 리소스 종류만 다르고 할당 형태는 같기 때문이다.
'''

import { roleAssignmentConfig } from './foundry-role-assignments.bicep'

@description('대상 워크스페이스 이름')
param workspaceName string

@description('역할 할당 목록. principalId가 빈 항목은 호출 측에서 걸러서 넘긴다.')
param assignments roleAssignmentConfig[]

resource workspace 'Microsoft.MachineLearningServices/workspaces@2024-10-01' existing = {
  name: workspaceName
}

resource roleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for assignment in assignments: {
    name: guid(workspace.id, assignment.principalId, assignment.roleDefinitionId)
    scope: workspace
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
