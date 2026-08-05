metadata description = '''
Route Table 모듈.

Private 망 워크로드 서브넷의 0.0.0.0/0을 Azure Firewall로 강제 터널링해서
아웃바운드가 반드시 방화벽의 FQDN 화이트리스트를 통과하도록 만든다.

주의: AzureBastionSubnet에는 0.0.0.0/0 UDR을 적용하면 안 된다(Bastion 제어 평면이 끊긴다).
'''

@description('Route Table 이름')
param name string

@description('배포 리전')
param location string

@description('리소스 태그')
param tags object = {}

@description('기본 경로(0.0.0.0/0)를 보낼 Azure Firewall의 프라이빗 IP')
param firewallPrivateIpAddress string

@description('추가 경로 목록')
param additionalRoutes array = []

@description('BGP 경로 전파를 비활성화할지 여부. 강제 터널링을 보장하려면 true를 권장한다.')
param disableBgpRoutePropagation bool = true

var defaultRoute = {
  name: 'default-to-firewall'
  properties: {
    addressPrefix: '0.0.0.0/0'
    nextHopType: 'VirtualAppliance'
    nextHopIpAddress: firewallPrivateIpAddress
  }
}

resource routeTable 'Microsoft.Network/routeTables@2025-07-01' = {
  name: name
  location: location
  tags: tags
  properties: {
    disableBgpRoutePropagation: disableBgpRoutePropagation
    routes: concat([defaultRoute], additionalRoutes)
  }
}

@description('Route Table 리소스 ID')
output id string = routeTable.id

@description('Route Table 이름')
output name string = routeTable.name
