metadata description = '''
Azure AI Foundry 모델 배포 모듈.

같은 계정에 여러 배포를 동시에 만들면 충돌하므로 @batchSize(1)로 순차 생성한다.
모델 배포는 제어 평면(ARM) 작업이라 계정이 publicNetworkAccess=Disabled 여도 정상 동작한다.
'''

@export()
@description('모델 배포 정의')
type modelDeploymentConfig = {
  @description('배포 이름. 애플리케이션에서 model 파라미터로 쓰는 값이다.')
  name: string

  @description('모델 이름. 예: gpt-5.4-mini. lifecycleStatus=GenerallyAvailable 인 모델만 배포된다.')
  modelName: string

  @description('모델 버전. 예: 2026-03-17')
  modelVersion: string

  @description('모델 형식')
  modelFormat: string?

  @description('배포 SKU. 예: GlobalStandard, Standard, DataZoneStandard')
  skuName: string?

  @description('할당 용량(분당 1,000 토큰 단위)')
  capacity: int?
}

@description('상위 Foundry 계정 이름')
param accountName string

@description('배포할 모델 목록')
param deployments modelDeploymentConfig[]

@description('적용할 Responsible AI 정책 이름')
param raiPolicyName string = 'Microsoft.DefaultV2'

resource account 'Microsoft.CognitiveServices/accounts@2026-05-01' existing = {
  name: accountName
}

@batchSize(1)
resource modelDeployment 'Microsoft.CognitiveServices/accounts/deployments@2026-05-01' = [
  for deployment in deployments: {
    parent: account
    name: deployment.name
    sku: {
      name: deployment.?skuName ?? 'GlobalStandard'
      capacity: deployment.?capacity ?? 20
    }
    properties: {
      model: {
        format: deployment.?modelFormat ?? 'OpenAI'
        name: deployment.modelName
        version: deployment.modelVersion
      }
      versionUpgradeOption: 'OnceNewDefaultVersionAvailable'
      raiPolicyName: raiPolicyName
    }
  }
]

@description('생성된 배포 이름 목록')
output deploymentNames string[] = map(deployments, deployment => deployment.name)
