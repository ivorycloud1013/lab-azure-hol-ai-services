metadata description = '''
Virtual Network 모듈.

subnetConfig 타입에서 networkSecurityGroupId를 선택 항목이 아닌 필수 항목으로 두어,
"모든 subnet에 NSG를 붙인다"는 기준을 컴파일 시점에 강제한다.
NSG를 붙일 수 없는 플랫폼 예외(AzureFirewallSubnet 등)만 빈 문자열을 넘기며,
그 경우 subnetsWithoutNsg 출력으로 예외 목록이 드러난다.
'''

@export()
@description('서브넷 정의')
type subnetConfig = {
  @description('서브넷 이름')
  name: string

  @description('서브넷 CIDR')
  addressPrefix: string

  @description('연결할 NSG 리소스 ID. 빈 문자열은 NSG를 지원하지 않는 플랫폼 서브넷에만 사용한다.')
  networkSecurityGroupId: string

  @description('연결할 Route Table 리소스 ID')
  routeTableId: string?

  @description('서비스 엔드포인트 목록. 예: [\'Microsoft.CognitiveServices\']')
  serviceEndpoints: string[]?

  @description('Private Endpoint 트래픽에 NSG/UDR을 적용할지 여부. Enabled면 NSG가 PE 트래픽에도 적용된다.')
  privateEndpointNetworkPolicies: ('Enabled' | 'Disabled')?

  @description('Private Link Service 네트워크 정책')
  privateLinkServiceNetworkPolicies: ('Enabled' | 'Disabled')?
}

@description('VNet 이름')
param name string

@description('배포 리전')
param location string

@description('리소스 태그')
param tags object = {}

@description('VNet 주소 공간')
param addressPrefixes string[]

@description('서브넷 목록')
param subnets subnetConfig[]

@description('VNet에 지정할 사용자 DNS 서버 목록. 비어 있으면 Azure 제공 DNS(168.63.129.16)를 사용한다.')
param dnsServers string[] = []

resource vnet 'Microsoft.Network/virtualNetworks@2025-07-01' = {
  name: name
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: addressPrefixes
    }
    dhcpOptions: empty(dnsServers) ? null : {
      dnsServers: dnsServers
    }
    subnets: [
      for subnet in subnets: {
        name: subnet.name
        properties: {
          addressPrefix: subnet.addressPrefix
          networkSecurityGroup: empty(subnet.networkSecurityGroupId) ? null : {
            id: subnet.networkSecurityGroupId
          }
          routeTable: empty(subnet.?routeTableId ?? '') ? null : {
            id: subnet.?routeTableId
          }
          // for-식은 중첩할 수 없으므로 map()으로 변환한다.
          serviceEndpoints: map(subnet.?serviceEndpoints ?? [], service => {
            service: service
          })
          privateEndpointNetworkPolicies: subnet.?privateEndpointNetworkPolicies ?? 'Disabled'
          privateLinkServiceNetworkPolicies: subnet.?privateLinkServiceNetworkPolicies ?? 'Enabled'
        }
      }
    ]
  }
}

@description('VNet 리소스 ID')
output id string = vnet.id

@description('VNet 이름')
output name string = vnet.name

@description('서브넷 이름 -> 리소스 ID 매핑')
output subnetIds object = toObject(
  subnets,
  subnet => subnet.name,
  subnet => resourceId('Microsoft.Network/virtualNetworks/subnets', name, subnet.name)
)

@description('NSG가 연결되지 않은 서브넷 이름 목록. 플랫폼 예외 외에는 비어 있어야 한다.')
output subnetsWithoutNsg string[] = map(
  filter(subnets, subnet => empty(subnet.networkSecurityGroupId)),
  subnet => subnet.name
)
