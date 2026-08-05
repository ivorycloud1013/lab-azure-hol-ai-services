metadata description = '''
Network Security Group 모듈.

"VNet에 붙는 모든 subnet은 NSG를 가져야 하고 deny-all이 기본" 이라는 보안 기준을 구현한다.
enableDenyAllBaseline=true 이면 우선순위 4096(사용자 규칙 최대값)에 Inbound/Outbound
Deny-All 규칙을 자동으로 덧붙인다. 이 규칙은 Azure 기본 규칙인
AllowVnetInBound(65000), AllowVnetOutBound(65000), AllowInternetOutBound(65001)
보다 우선순위가 높으므로, 결과적으로 "명시적으로 허용한 것만 통과"하는 상태가 된다.
'''

@description('NSG 이름')
param name string

@description('배포 리전')
param location string

@description('리소스 태그')
param tags object = {}

@description('명시적 허용/거부 규칙. deny-all 기준선(4096)보다 낮은 우선순위 번호(100~4000)를 사용해야 한다.')
param securityRules array = []

@description('true이면 우선순위 4096에 Deny-All Inbound/Outbound 기준선을 추가한다.')
param enableDenyAllBaseline bool = true

@description('진단 로그를 보낼 Log Analytics 작업 영역 ID. 빈 문자열이면 진단 설정을 만들지 않는다.')
param logAnalyticsWorkspaceId string = ''

// 사용자 정의 NSG 규칙에 허용되는 최대 우선순위. 이보다 큰 번호는 Azure 기본 규칙 영역이다.
var DENY_ALL_PRIORITY = 4096

var denyAllBaselineRules = [
  {
    name: 'DenyAllInbound'
    properties: {
      description: 'Deny-all 기준선. 명시적으로 허용되지 않은 모든 인바운드를 차단한다.'
      priority: DENY_ALL_PRIORITY
      direction: 'Inbound'
      access: 'Deny'
      protocol: '*'
      sourceAddressPrefix: '*'
      sourcePortRange: '*'
      destinationAddressPrefix: '*'
      destinationPortRange: '*'
    }
  }
  {
    name: 'DenyAllOutbound'
    properties: {
      description: 'Deny-all 기준선. 기본 규칙 AllowVnetOutBound/AllowInternetOutBound를 무력화한다.'
      priority: DENY_ALL_PRIORITY
      direction: 'Outbound'
      access: 'Deny'
      protocol: '*'
      sourceAddressPrefix: '*'
      sourcePortRange: '*'
      destinationAddressPrefix: '*'
      destinationPortRange: '*'
    }
  }
]

resource nsg 'Microsoft.Network/networkSecurityGroups@2025-07-01' = {
  name: name
  location: location
  tags: tags
  properties: {
    securityRules: concat(securityRules, enableDenyAllBaseline ? denyAllBaselineRules : [])
  }
}

resource diagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = if (!empty(logAnalyticsWorkspaceId)) {
  name: 'diag-to-law'
  scope: nsg
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [
      {
        categoryGroup: 'allLogs'
        enabled: true
      }
    ]
  }
}

@description('NSG 리소스 ID')
output id string = nsg.id

@description('NSG 이름')
output name string = nsg.name
