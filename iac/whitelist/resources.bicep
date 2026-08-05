metadata description = '''
화이트리스트 리소스 (리소스 그룹 범위).

Firewall Policy(규칙)와 Azure Firewall(어플라이언스)을 함께 소유한다.
둘을 갈라 놓으면 방화벽 생성 시점에 정책이 필요해 배포 순서가 꼬이므로 한 스택에 둔다.
'''

@description('리소스 이름 접두사')
param namePrefix string

@description('배포 리전')
param location string

@description('리소스 태그')
param tags object = {}

@description('Azure Firewall SKU')
@allowed(['Basic', 'Standard', 'Premium'])
param firewallSkuTier string = 'Basic'

@description('AzureFirewallSubnet 리소스 ID (private 스택 소유)')
param firewallSubnetId string

@description('AzureFirewallManagementSubnet 리소스 ID. Basic SKU에서만 필요하며 그 외에는 빈 문자열.')
param firewallManagementSubnetId string = ''

@description('방화벽 규칙의 출발지 CIDR 목록')
param protectedSourceAddresses string[]

@description('L4로 허용할 Azure 서비스 태그 목록')
param allowedServiceTags string[]

@description('Entra ID 인증 및 Azure 관리 평면 FQDN 화이트리스트')
param identityAndManagementFqdns string[]

@description('Azure AI Foundry FQDN 화이트리스트')
param foundryFqdns string[]

@description('Azure Portal FQDN 화이트리스트')
param portalFqdns string[]

@description('실습 도구 FQDN 화이트리스트')
param toolingFqdns string[]

@description('추가 FQDN 화이트리스트')
param additionalAllowedFqdns string[] = []

@description('Azure Firewall FQDN 태그 화이트리스트')
param allowedFqdnTags string[]

@description('위협 인텔리전스 모드')
@allowed(['Off', 'Alert', 'Deny'])
param threatIntelMode string = 'Alert'

@description('진단 로그를 보낼 Log Analytics 작업 영역 ID')
param logAnalyticsWorkspaceId string = ''

module firewallPolicy '../modules/network/firewall-policy.bicep' = {
  name: 'whitelist-firewall-policy'
  params: {
    name: 'afwp-${namePrefix}-whitelist'
    location: location
    tags: tags
    skuTier: firewallSkuTier
    sourceAddresses: protectedSourceAddresses
    allowedServiceTags: allowedServiceTags
    identityAndManagementFqdns: identityAndManagementFqdns
    foundryFqdns: foundryFqdns
    portalFqdns: portalFqdns
    toolingFqdns: toolingFqdns
    additionalAllowedFqdns: additionalAllowedFqdns
    allowedFqdnTags: allowedFqdnTags
    threatIntelMode: threatIntelMode
  }
}

module firewall '../modules/network/firewall.bicep' = {
  name: 'whitelist-firewall'
  params: {
    name: 'afw-${namePrefix}-whitelist'
    location: location
    tags: tags
    skuTier: firewallSkuTier
    subnetId: firewallSubnetId
    managementSubnetId: firewallManagementSubnetId
    firewallPolicyId: firewallPolicy.outputs.id
    logAnalyticsWorkspaceId: logAnalyticsWorkspaceId
  }
}

@description('Firewall Policy 이름')
output firewallPolicyName string = firewallPolicy.outputs.name

@description('Azure Firewall 이름')
output firewallName string = firewall.outputs.name

@description('Azure Firewall 사설 IP')
output firewallPrivateIpAddress string = firewall.outputs.privateIpAddress

@description('Azure Firewall 공인 IP')
output firewallPublicIpAddress string = firewall.outputs.publicIpAddress
