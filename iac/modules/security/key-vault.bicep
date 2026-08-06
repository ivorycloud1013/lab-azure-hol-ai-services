metadata description = '''
Key Vault 모듈.

Azure Machine Learning 워크스페이스가 연결 문자열과 자격 증명을 보관하는 데 사용한다.
접근 정책(access policy) 대신 RBAC 인증을 쓰고(enableRbacAuthorization=true),
실제 접근은 vault Private Endpoint 로만 이루어진다.

네트워크 설정은 storage-account.bicep 과 같은 이유로 같은 조합을 쓴다.
publicNetworkAccess 를 Disabled 로 내리면 networkAcls.bypass 가 평가되지 않아
AML 리소스 공급자가 워크스페이스를 프로비저닝할 때 자격 증명 모음에 닿지 못한다.
defaultAction=Deny 이고 허용 목록이 비어 있으므로 인터넷에서 들어올 수 있는 클라이언트는 없다.

purge protection 은 켜지 않는다. 한 번 켜면 되돌릴 수 없어서, 실습 환경을 지우고 다시 만들 때
같은 이름을 90일 동안 쓰지 못하게 되기 때문이다.

주의 — 소프트 삭제(soft delete)는 Azure가 강제하는 기능이라 끌 수 없다.
리소스 그룹을 지운 뒤 같은 이름으로 다시 배포하면 "이미 삭제 대기 중인 자격 증명 모음"
오류가 난다. 이때는 az keyvault purge --name <이름> --location <리전> 으로 완전히 지운다.
'''

@description('Key Vault 이름. 전역 고유해야 하며 3~24자 영문/숫자/하이픈만 쓸 수 있다.')
@minLength(3)
@maxLength(24)
param name string

@description('배포 리전')
param location string

@description('리소스 태그')
param tags object = {}

@description('공용 엔드포인트의 존재 여부. Disabled 로 두면 AML 워크스페이스를 만들 수 없다(위 설명 참고).')
@allowed(['Enabled', 'Disabled'])
param publicNetworkAccess string = 'Enabled'

@description('소프트 삭제 보존 기간(일). 7~90일. 실습 환경은 최소값을 써서 재배포를 쉽게 한다.')
@minValue(7)
@maxValue(90)
param softDeleteRetentionInDays int = 7

@description('네트워크 ACL 우회 범위. AML 리소스 공급자가 통과해야 하므로 AzureServices 가 필요하다.')
@allowed(['None', 'AzureServices'])
param networkAclsBypass string = 'AzureServices'

resource keyVault 'Microsoft.KeyVault/vaults@2024-11-01' = {
  name: name
  location: location
  tags: tags
  properties: {
    tenantId: subscription().tenantId
    sku: {
      family: 'A'
      name: 'standard'
    }
    // 접근 정책을 쓰지 않고 Azure RBAC 로만 권한을 준다. Foundry 의 keyless 원칙과 같은 방향이다.
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: softDeleteRetentionInDays
    publicNetworkAccess: publicNetworkAccess
    networkAcls: {
      defaultAction: 'Deny'
      bypass: networkAclsBypass
      ipRules: []
      virtualNetworkRules: []
    }
  }
}

@description('Key Vault 리소스 ID')
output id string = keyVault.id

@description('Key Vault 이름')
output name string = keyVault.name
