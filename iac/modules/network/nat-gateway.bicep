metadata description = '''
NAT Gateway + 전용 공인 IP 모듈.

왜 필요한가
  공인 IP 도, NAT Gateway 도, Load Balancer 아웃바운드 규칙도 없는 VM 은 인터넷으로 나갈 수 없다.
  예전에는 Azure 가 default outbound access 라는 이름으로 암묵적인 SNAT 를 붙여 줬지만,
  이 동작은 2025년 9월 30일자로 신규 배포에 대해 폐지됐다.
  따라서 지금 만드는 서브넷의 VM 은 아웃바운드 경로를 명시적으로 지정해야 한다.

왜 공인 IP 를 VM 에 직접 붙이지 않는가
  VM 에 공인 IP 를 붙이면 VM 자체가 인터넷에 주소를 갖게 된다. NSG 로 인바운드를 막더라도
  "Private 망 실습" 이라는 이 시스템의 전제가 흐려진다.
  NAT Gateway 는 아웃바운드 전용이라 인바운드 노출을 전혀 만들지 않으면서,
  나가는 트래픽을 고정된 공인 IP 하나로 SNAT 한다.
  고객사 방화벽에 "이 환경이 나가는 IP" 를 제출해야 할 때도 이 주소 하나면 된다.

주의 — AzureBastionSubnet 에는 연결하면 안 된다. Bastion 은 자체 공인 IP 로 아웃바운드를
처리하며, NAT Gateway 를 붙이면 제어 평면 동작이 어긋난다.
'''

@description('NAT Gateway 이름')
param name string

@description('배포 리전')
param location string

@description('리소스 태그')
param tags object = {}

@description('''
유휴 연결 타임아웃(분). 4~120.
값을 올리면 연결을 오래 붙잡아 SNAT 포트가 늦게 반환되므로, 기본값을 그대로 두는 편이 낫다.
''')
@minValue(4)
@maxValue(120)
param idleTimeoutInMinutes int = 4

// NAT Gateway 는 Standard SKU 공인 IP 만 받는다. 할당 방식도 Static 이어야 한다.
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

resource natGateway 'Microsoft.Network/natGateways@2025-07-01' = {
  name: name
  location: location
  tags: tags
  sku: {
    name: 'Standard'
  }
  properties: {
    idleTimeoutInMinutes: idleTimeoutInMinutes
    publicIpAddresses: [
      {
        id: publicIp.id
      }
    ]
  }
}

@description('NAT Gateway 리소스 ID. 서브넷의 natGateway 속성에 넘긴다.')
output id string = natGateway.id

@description('NAT Gateway 이름')
output name string = natGateway.name

@description('아웃바운드 트래픽이 SNAT 되는 공인 IP 주소')
output publicIpAddress string = publicIp.properties.ipAddress
