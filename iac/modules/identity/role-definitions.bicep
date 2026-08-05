metadata description = '''
실습에서 사용하는 Azure 기본 제공 역할 정의 GUID 모음.

Keyless(Entra ID) 인증에서는 이 역할들이 유일한 접근 통제 수단이다.
GUID는 az role definition list --name "<역할 이름>" --query "[0].name" 으로 확인했다.
'''

@export()
@description('Cognitive Services User - AI Services 데이터 평면 호출 및 키 조회')
var COGNITIVE_SERVICES_USER_ROLE_ID = 'a97b65f3-24c7-4388-baec-2e87135dc908'

@export()
@description('Cognitive Services OpenAI User - OpenAI 모델 추론 호출')
var COGNITIVE_SERVICES_OPENAI_USER_ROLE_ID = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'

@export()
@description('Cognitive Services OpenAI Contributor - 모델 배포 등 관리 작업 포함')
var COGNITIVE_SERVICES_OPENAI_CONTRIBUTOR_ROLE_ID = 'a001fd3d-188f-4b5d-821b-7da978bf7442'

@export()
@description('Azure AI Developer - Foundry 프로젝트 작업')
var AZURE_AI_DEVELOPER_ROLE_ID = '64702f94-c441-49e6-a78b-ef80e0188fee'
