metadata description = '''
Private DNS Zone + VNet 링크 모듈.

Private Endpoint의 이름 해석을 위해 privatelink.* 존을 만들고 Private 망 VNet에 연결한다.
VNet의 DNS를 Azure 제공 DNS(168.63.129.16)로 두면 링크된 존이 자동으로 조회되므로,
점프박스에서 Foundry FQDN이 사설 IP로 해석된다.
'''

@description('Private DNS Zone 이름. 예: privatelink.openai.azure.com')
param name string

@description('리소스 태그')
param tags object = {}

@description('존을 연결할 VNet 리소스 ID')
param virtualNetworkId string

@description('VNet 링크에 자동 등록을 켤지 여부. Private Endpoint 용도에서는 false여야 한다.')
param registrationEnabled bool = false

@description('''
존을 VNet에 연결할지 여부. 기본값은 true다.

false로 두면 존과 A 레코드는 그대로 만들어지지만 VNet에서 조회되지 않는다.
점프박스를 on-prem 노트북처럼 만들 때 쓴다. on-prem 클라이언트는 Azure Private DNS Zone을
볼 수 없으므로, 모든 이름이 공용으로 해석되고 모든 트래픽이 방화벽을 거친다.
그래야 방화벽 로그로 고객사에 제출할 FQDN 목록을 빠짐없이 뽑을 수 있다.
''')
param linkToVirtualNetwork bool = true

resource zone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: name
  location: 'global'
  tags: tags
}

resource link 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = if (linkToVirtualNetwork) {
  parent: zone
  name: 'link-${uniqueString(virtualNetworkId)}'
  location: 'global'
  tags: tags
  properties: {
    registrationEnabled: registrationEnabled
    virtualNetwork: {
      id: virtualNetworkId
    }
  }
}

@description('Private DNS Zone 리소스 ID')
output id string = zone.id

@description('Private DNS Zone 이름')
output name string = zone.name
