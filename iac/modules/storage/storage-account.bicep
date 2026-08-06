metadata description = '''
Storage Account 모듈.

Azure Machine Learning 워크스페이스의 기본 데이터스토어로 사용한다.
데이터 접근은 blob / file Private Endpoint 로만 이루어진다.

왜 publicNetworkAccess=Disabled 가 아닌가
  Foundry 계정은 publicNetworkAccess=Disabled + bypass=None 으로 완전히 잠근다.
  하지만 AML 의 연결 리소스에 같은 설정을 쓰면 워크스페이스를 만들 수 없다.
  Azure Machine Learning 리소스 공급자가 워크스페이스를 프로비저닝할 때
  "신뢰할 수 있는 Azure 서비스" 자격으로 스토리지에 접근해야 하는데,
  publicNetworkAccess=Disabled 는 공용 엔드포인트 자체를 없애 버려서 networkAcls.bypass 가
  아예 평가되지 않기 때문이다.

  그래서 Microsoft 공식 보안 레퍼런스 템플릿과 같은 조합을 쓴다.
    publicNetworkAccess = Enabled  (공용 엔드포인트는 존재)
    networkAcls.defaultAction = Deny  (허용 목록이 비어 있으므로 실제로는 아무도 못 들어옴)
    networkAcls.bypass = AzureServices  (AML 리소스 공급자만 통과)

  ipRules 와 virtualNetworkRules 를 비워 두므로, 결과적으로 인터넷의 어떤 클라이언트도
  이 계정에 도달할 수 없다. 실제 데이터 경로는 Private Endpoint 뿐이다.
'''

@description('스토리지 계정 이름. 소문자와 숫자만 쓸 수 있고 전역 고유해야 한다.')
@minLength(3)
@maxLength(24)
param name string

@description('배포 리전')
param location string

@description('리소스 태그')
param tags object = {}

@description('복제 SKU')
@allowed(['Standard_LRS', 'Standard_ZRS', 'Standard_GRS'])
param skuName string = 'Standard_LRS'

@description('''
공용 엔드포인트의 존재 여부. Disabled 로 두면 AML 워크스페이스를 만들 수 없다(위 설명 참고).
공용 엔드포인트가 있어도 networkAcls.defaultAction=Deny 라 실제로 들어올 수 있는 클라이언트는 없다.
''')
@allowed(['Enabled', 'Disabled'])
param publicNetworkAccess string = 'Enabled'

@description('네트워크 ACL 우회 범위. AML 리소스 공급자가 통과해야 하므로 AzureServices 가 필요하다.')
@allowed(['None', 'AzureServices'])
param networkAclsBypass string = 'AzureServices'

resource storageAccount 'Microsoft.Storage/storageAccounts@2024-01-01' = {
  name: name
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: {
    name: skuName
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
    allowCrossTenantReplication: false
    // AML 은 기본 설정(systemDatastoresAuthMode=accessKey)에서 노트북 파일 공유에 스토리지 키를 쓴다.
    // 키를 끄면 워크스페이스 프로비저닝과 컴퓨팅 인스턴스 노트북이 실패하므로 켜 둔다.
    // 공용 엔드포인트가 닫혀 있어, 키를 알아도 Private Endpoint 없이는 접근할 수 없다.
    allowSharedKeyAccess: true
    publicNetworkAccess: publicNetworkAccess
    networkAcls: {
      defaultAction: 'Deny'
      bypass: networkAclsBypass
      ipRules: []
      virtualNetworkRules: []
    }
  }
}

@description('스토리지 계정 리소스 ID')
output id string = storageAccount.id

@description('스토리지 계정 이름')
output name string = storageAccount.name
