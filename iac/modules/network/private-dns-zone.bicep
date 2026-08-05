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

resource zone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: name
  location: 'global'
  tags: tags
}

resource link 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
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
