metadata description = '''
Azure Bastion 모듈. 공인 IP가 없는 Private 망 점프박스에 접속하는 유일한 경로다.

AzureBastionSubnet은 /26 이상이어야 하고, NSG를 반드시 연결할 수 있다(연결해야 한다).
필요한 NSG 규칙은 zones/private-zone.bicep 에 정의되어 있다.
'''

@description('Bastion 호스트 이름')
param name string

@description('배포 리전')
param location string

@description('리소스 태그')
param tags object = {}

@description('Bastion SKU. Basic은 기본 RDP/SSH만, Standard는 네이티브 클라이언트/터널링을 지원한다.')
@allowed(['Basic', 'Standard'])
param skuName string = 'Basic'

@description('AzureBastionSubnet 리소스 ID')
param subnetId string

@description('진단 로그를 보낼 Log Analytics 작업 영역 ID. 빈 문자열이면 진단 설정을 만들지 않는다.')
param logAnalyticsWorkspaceId string = ''

var isStandardSku = skuName == 'Standard'

resource publicIp 'Microsoft.Network/publicIPAddresses@2025-07-01' = {
  name: 'pip-${name}'
  location: location
  tags: tags
  sku: {
    name: 'Standard'
  }
  properties: {
    publicIPAllocationMethod: 'Static'
    publicIPAddressVersion: 'IPv4'
  }
}

resource bastion 'Microsoft.Network/bastionHosts@2025-07-01' = {
  name: name
  location: location
  tags: tags
  sku: {
    name: skuName
  }
  properties: {
    // Basic SKU는 아래 기능 플래그를 지원하지 않으므로 Standard일 때만 켠다.
    enableTunneling: isStandardSku ? true : null
    enableIpConnect: isStandardSku ? true : null
    ipConfigurations: [
      {
        name: 'ipconfig-bastion'
        properties: {
          subnet: {
            id: subnetId
          }
          publicIPAddress: {
            id: publicIp.id
          }
        }
      }
    ]
  }
}

resource diagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = if (!empty(logAnalyticsWorkspaceId)) {
  name: 'diag-to-law'
  scope: bastion
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

@description('Bastion 리소스 ID')
output id string = bastion.id

@description('Bastion 이름')
output name string = bastion.name
