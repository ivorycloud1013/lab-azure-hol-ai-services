metadata description = '''
Azure Firewall 모듈.

Basic SKU는 데이터 평면용 AzureFirewallSubnet 외에 AzureFirewallManagementSubnet과
별도의 공인 IP를 반드시 요구한다. Standard/Premium은 강제 터널링을 쓰지 않는 한 불필요하다.

AzureFirewallSubnet / AzureFirewallManagementSubnet에는 NSG를 연결할 수 없다(플랫폼 제약).
이 두 서브넷이 "모든 서브넷에 NSG" 기준의 유일한 예외다.
'''

@description('Azure Firewall 이름')
param name string

@description('배포 리전')
param location string

@description('리소스 태그')
param tags object = {}

@description('Azure Firewall SKU')
@allowed(['Basic', 'Standard', 'Premium'])
param skuTier string = 'Basic'

@description('AzureFirewallSubnet 리소스 ID')
param subnetId string

@description('AzureFirewallManagementSubnet 리소스 ID. Basic SKU에서는 필수, 그 외에는 빈 문자열.')
param managementSubnetId string = ''

@description('연결할 Firewall Policy 리소스 ID')
param firewallPolicyId string

@description('진단 로그를 보낼 Log Analytics 작업 영역 ID. 빈 문자열이면 진단 설정을 만들지 않는다.')
param logAnalyticsWorkspaceId string = ''

var hasManagementSubnet = !empty(managementSubnetId)

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

resource managementPublicIp 'Microsoft.Network/publicIPAddresses@2025-07-01' = if (hasManagementSubnet) {
  name: 'pip-${name}-mgmt'
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

resource firewall 'Microsoft.Network/azureFirewalls@2025-07-01' = {
  name: name
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'AZFW_VNet'
      tier: skuTier
    }
    firewallPolicy: {
      id: firewallPolicyId
    }
    ipConfigurations: [
      {
        name: 'ipconfig-data'
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
    managementIpConfiguration: hasManagementSubnet ? {
      name: 'ipconfig-management'
      properties: {
        subnet: {
          id: managementSubnetId
        }
        publicIPAddress: {
          id: managementPublicIp.id
        }
      }
    } : null
  }
}

resource diagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = if (!empty(logAnalyticsWorkspaceId)) {
  name: 'diag-to-law'
  scope: firewall
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [
      {
        categoryGroup: 'allLogs'
        enabled: true
      }
    ]
    metrics: [
      {
        category: 'AllMetrics'
        enabled: true
      }
    ]
  }
}

@description('Azure Firewall 리소스 ID')
output id string = firewall.id

@description('Azure Firewall 이름')
output name string = firewall.name

@description('UDR의 next hop으로 사용할 방화벽 프라이빗 IP')
output privateIpAddress string = firewall.properties.ipConfigurations[0].properties.privateIPAddress

@description('방화벽 아웃바운드 공인 IP (SNAT 주소)')
output publicIpAddress string = publicIp.properties.ipAddress
