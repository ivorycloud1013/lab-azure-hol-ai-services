metadata description = 'Private Endpoint + Private DNS Zone Group 모듈.'

@description('Private Endpoint 이름')
param name string

@description('배포 리전')
param location string

@description('리소스 태그')
param tags object = {}

@description('Private Endpoint를 배치할 서브넷 리소스 ID')
param subnetId string

@description('연결 대상 리소스 ID')
param targetResourceId string

@description('연결 대상의 group ID 목록. Azure AI Foundry(CognitiveServices)는 [\'account\'].')
param groupIds string[]

@description('A 레코드를 등록할 Private DNS Zone 리소스 ID 목록')
param privateDnsZoneIds string[] = []

resource privateEndpoint 'Microsoft.Network/privateEndpoints@2025-07-01' = {
  name: name
  location: location
  tags: tags
  properties: {
    subnet: {
      id: subnetId
    }
    privateLinkServiceConnections: [
      {
        name: '${name}-connection'
        properties: {
          privateLinkServiceId: targetResourceId
          groupIds: groupIds
        }
      }
    ]
  }
}

resource dnsZoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2025-07-01' = if (!empty(privateDnsZoneIds)) {
  parent: privateEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      for (zoneId, index) in privateDnsZoneIds: {
        name: 'config-${index}'
        properties: {
          privateDnsZoneId: zoneId
        }
      }
    ]
  }
}

@description('Private Endpoint 리소스 ID')
output id string = privateEndpoint.id

@description('Private Endpoint 이름')
output name string = privateEndpoint.name
