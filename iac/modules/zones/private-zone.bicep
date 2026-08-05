metadata description = '''
Private 망 존. "Azure조차 기본 차단, 화이트리스트로만 뚫는다"를 구현한다.

접근 경로: 실습자 노트북 -> Azure Bastion -> 점프박스 VM -> Private Endpoint -> Foundry

3중 통제
  1) NSG          : 모든 서브넷에 연결, 우선순위 4096 deny-all 기준선.
                    명시적으로 허용한 출발지/목적지/포트만 통과한다.
  2) Azure Firewall: 워크로드 서브넷의 0.0.0.0/0을 UDR로 강제 터널링하고,
                    L7 FQDN 화이트리스트로 아웃바운드를 통제한다.
  3) 서비스 방화벽 : Foundry는 publicNetworkAccess=Disabled, networkAcls bypass=None.
                    "신뢰할 수 있는 Azure 서비스"도 우회하지 못하고 Private Endpoint로만 접근한다.

NSG 예외
  AzureFirewallSubnet 과 AzureFirewallManagementSubnet 은 Azure 플랫폼이 NSG 연결을
  지원하지 않는다. 이 두 개가 "모든 서브넷에 NSG" 기준의 유일한 예외이며,
  subnetsWithoutNsg 출력으로 예외 목록이 드러난다.

UDR next hop
  Azure Firewall은 전용 AzureFirewallSubnet에서 항상 첫 할당 가능 주소(x.x.x.4)를 받는다.
  (.0 네트워크 / .1 게이트웨이 / .2 .3 Azure 예약)
  따라서 cidrHost(prefix, 3)으로 계산해 방화벽 생성 전에 Route Table을 만들 수 있고,
  VNet <-> Firewall <-> RouteTable 간 순환 의존을 피한다.
  계산값과 실제값은 firewallPrivateIpMatchesRoute 출력으로 검증한다.
'''

import { subnetConfig } from '../network/vnet.bicep'
import { modelDeploymentConfig } from '../ai/model-deployments.bicep'
import { roleAssignmentConfig } from '../identity/foundry-role-assignments.bicep'
import {
  COGNITIVE_SERVICES_USER_ROLE_ID
  COGNITIVE_SERVICES_OPENAI_USER_ROLE_ID
  AZURE_AI_DEVELOPER_ROLE_ID
} from '../identity/role-definitions.bicep'

@description('리소스 이름 접두사')
param namePrefix string

@description('전역 고유 이름에 사용할 토큰')
param resourceToken string

@description('배포 리전')
param location string

@description('리소스 태그')
param tags object = {}

@description('Private VNet 주소 공간')
param vnetAddressPrefix string = '10.20.0.0/16'

@description('AzureFirewallSubnet CIDR. /26 이상이어야 한다.')
param firewallSubnetPrefix string = '10.20.0.0/26'

@description('AzureFirewallManagementSubnet CIDR. Basic SKU에서 필수다.')
param firewallManagementSubnetPrefix string = '10.20.0.64/26'

@description('AzureBastionSubnet CIDR. /26 이상이어야 한다.')
param bastionSubnetPrefix string = '10.20.0.128/26'

@description('Private Endpoint 서브넷 CIDR')
param privateEndpointSubnetPrefix string = '10.20.1.0/24'

@description('점프박스 서브넷 CIDR')
param jumpboxSubnetPrefix string = '10.20.2.0/24'

@description('Azure Firewall SKU. Basic은 관리 서브넷과 공인 IP가 추가로 필요하다.')
@allowed(['Basic', 'Standard', 'Premium'])
param firewallSkuTier string = 'Basic'

@description('Azure Bastion SKU')
@allowed(['Basic', 'Standard'])
param bastionSkuName string = 'Basic'

@description('방화벽 FQDN 화이트리스트에 추가할 도메인')
param additionalAllowedFqdns string[] = []

@description('Private Endpoint 서브넷에 NSG/UDR을 적용할지 여부. Enabled면 NSG가 PE 트래픽에도 적용된다.')
@allowed(['Enabled', 'Disabled'])
param privateEndpointNetworkPolicies string = 'Enabled'

@description('점프박스 VM 크기')
param vmSize string = 'Standard_D2s_v5'

@description('점프박스 관리자 계정 이름')
param vmAdminUsername string

@secure()
@description('점프박스 관리자 비밀번호')
param vmAdminPassword string

@description('권한을 부여할 실습자 Entra 오브젝트 ID. 빈 값이면 실습자 역할 할당을 건너뛴다.')
param labUserPrincipalId string = ''

@description('실습자 보안 주체 유형')
@allowed(['User', 'Group', 'ServicePrincipal'])
param labUserPrincipalType string = 'User'

@description('Foundry 프로젝트 이름')
param projectName string = 'proj-private'

@description('배포할 모델 목록')
param modelDeployments modelDeploymentConfig[]

@description('진단 로그를 보낼 Log Analytics 작업 영역 ID')
param logAnalyticsWorkspaceId string = ''

// ---------------------------------------------------------------------------
// 상수
// ---------------------------------------------------------------------------

// Azure Firewall이 전용 서브넷에서 받는 첫 할당 가능 주소의 인덱스(.0 .1 .2 .3 예약 이후).
var FIREWALL_HOST_INDEX = 3

// Azure 플랫폼 DNS. VNet에 링크된 Private DNS Zone도 이 주소를 통해 조회된다.
var AZURE_PLATFORM_DNS_IP = '168.63.129.16'

var privateEndpointSubnetName = 'snet-private-endpoint'
var jumpboxSubnetName = 'snet-jumpbox'
var bastionSubnetName = 'AzureBastionSubnet'
var firewallSubnetName = 'AzureFirewallSubnet'
var firewallManagementSubnetName = 'AzureFirewallManagementSubnet'

var requiresFirewallManagementSubnet = firewallSkuTier == 'Basic'
var firewallPrivateIpAddress = cidrHost(firewallSubnetPrefix, FIREWALL_HOST_INDEX)

// Foundry Private Endpoint 이름 해석에 필요한 존. AI Foundry는 세 개 모두 필요하다.
var privateDnsZoneNames = [
  'privatelink.cognitiveservices.azure.com'
  'privatelink.openai.azure.com'
  'privatelink.services.ai.azure.com'
]

// ---------------------------------------------------------------------------
// NSG - 모든 서브넷에 연결, deny-all 기준선 위에 최소 허용만 얹는다
// ---------------------------------------------------------------------------

// Azure Bastion이 동작하기 위해 반드시 필요한 규칙 집합.
// 하나라도 빠지면 Bastion 프로비저닝 또는 세션 연결이 실패한다.
var bastionNsgRules = [
  {
    name: 'AllowHttpsInbound'
    properties: {
      description: '실습자 브라우저에서 Bastion으로 들어오는 HTTPS.'
      priority: 100
      direction: 'Inbound'
      access: 'Allow'
      protocol: 'Tcp'
      sourceAddressPrefix: 'Internet'
      sourcePortRange: '*'
      destinationAddressPrefix: '*'
      destinationPortRange: '443'
    }
  }
  {
    name: 'AllowGatewayManagerInbound'
    properties: {
      description: 'Bastion 제어 평면.'
      priority: 110
      direction: 'Inbound'
      access: 'Allow'
      protocol: 'Tcp'
      sourceAddressPrefix: 'GatewayManager'
      sourcePortRange: '*'
      destinationAddressPrefix: '*'
      destinationPortRange: '443'
    }
  }
  {
    name: 'AllowAzureLoadBalancerInbound'
    properties: {
      description: 'Bastion 상태 프로브.'
      priority: 120
      direction: 'Inbound'
      access: 'Allow'
      protocol: 'Tcp'
      sourceAddressPrefix: 'AzureLoadBalancer'
      sourcePortRange: '*'
      destinationAddressPrefix: '*'
      destinationPortRange: '443'
    }
  }
  {
    name: 'AllowBastionHostCommunicationInbound'
    properties: {
      description: 'Bastion 인스턴스 간 데이터 평면 통신.'
      priority: 130
      direction: 'Inbound'
      access: 'Allow'
      protocol: '*'
      sourceAddressPrefix: 'VirtualNetwork'
      sourcePortRange: '*'
      destinationAddressPrefix: 'VirtualNetwork'
      destinationPortRanges: ['8080', '5701']
    }
  }
  {
    name: 'AllowSshRdpOutbound'
    properties: {
      description: 'Bastion에서 점프박스로의 RDP/SSH.'
      priority: 100
      direction: 'Outbound'
      access: 'Allow'
      protocol: '*'
      sourceAddressPrefix: '*'
      sourcePortRange: '*'
      destinationAddressPrefix: 'VirtualNetwork'
      destinationPortRanges: ['22', '3389']
    }
  }
  {
    name: 'AllowAzureCloudOutbound'
    properties: {
      description: 'Bastion 종속 Azure 서비스 호출.'
      priority: 110
      direction: 'Outbound'
      access: 'Allow'
      protocol: 'Tcp'
      sourceAddressPrefix: '*'
      sourcePortRange: '*'
      destinationAddressPrefix: 'AzureCloud'
      destinationPortRange: '443'
    }
  }
  {
    name: 'AllowBastionCommunicationOutbound'
    properties: {
      description: 'Bastion 인스턴스 간 데이터 평면 통신.'
      priority: 120
      direction: 'Outbound'
      access: 'Allow'
      protocol: '*'
      sourceAddressPrefix: 'VirtualNetwork'
      sourcePortRange: '*'
      destinationAddressPrefix: 'VirtualNetwork'
      destinationPortRanges: ['8080', '5701']
    }
  }
  {
    name: 'AllowGetSessionInformationOutbound'
    properties: {
      description: 'Bastion 세션 정보 조회.'
      priority: 130
      direction: 'Outbound'
      access: 'Allow'
      protocol: '*'
      sourceAddressPrefix: '*'
      sourcePortRange: '*'
      destinationAddressPrefix: 'Internet'
      destinationPortRanges: ['80', '443']
    }
  }
]

var privateEndpointNsgRules = [
  {
    name: 'AllowJumpboxHttpsInbound'
    properties: {
      description: '점프박스 서브넷에서 Private Endpoint로의 HTTPS만 허용.'
      priority: 100
      direction: 'Inbound'
      access: 'Allow'
      protocol: 'Tcp'
      sourceAddressPrefix: jumpboxSubnetPrefix
      sourcePortRange: '*'
      destinationAddressPrefix: privateEndpointSubnetPrefix
      destinationPortRange: '443'
    }
  }
]

// NSG는 next hop이 아니라 패킷의 원래 목적지로 평가된다.
// 따라서 UDR로 방화벽에 보내는 트래픽도 NSG에서는 원래 목적지(Internet 등)로 허용해야 한다.
// L4 허용은 여기서 넓게 두고, 실제 FQDN 화이트리스트는 Azure Firewall이 담당한다.
var jumpboxNsgRules = [
  {
    name: 'AllowBastionInbound'
    properties: {
      description: 'Bastion 서브넷에서만 RDP/SSH 인바운드를 허용.'
      priority: 100
      direction: 'Inbound'
      access: 'Allow'
      protocol: 'Tcp'
      sourceAddressPrefix: bastionSubnetPrefix
      sourcePortRange: '*'
      destinationAddressPrefix: jumpboxSubnetPrefix
      destinationPortRanges: ['22', '3389']
    }
  }
  {
    name: 'AllowPrivateEndpointOutbound'
    properties: {
      description: 'Private Endpoint 경유 Foundry 호출.'
      priority: 100
      direction: 'Outbound'
      access: 'Allow'
      protocol: 'Tcp'
      sourceAddressPrefix: jumpboxSubnetPrefix
      sourcePortRange: '*'
      destinationAddressPrefix: privateEndpointSubnetPrefix
      destinationPortRange: '443'
    }
  }
  {
    name: 'AllowAzurePlatformDnsOutbound'
    properties: {
      description: 'Azure 제공 DNS 조회. Private DNS Zone 해석에 필요하다.'
      priority: 110
      direction: 'Outbound'
      access: 'Allow'
      protocol: '*'
      sourceAddressPrefix: jumpboxSubnetPrefix
      sourcePortRange: '*'
      destinationAddressPrefix: '${AZURE_PLATFORM_DNS_IP}/32'
      destinationPortRange: '53'
    }
  }
  {
    name: 'AllowInternetViaFirewallOutbound'
    properties: {
      description: 'UDR로 방화벽에 강제 터널링되는 HTTP/HTTPS. FQDN 통제는 방화벽이 수행한다.'
      priority: 120
      direction: 'Outbound'
      access: 'Allow'
      protocol: 'Tcp'
      sourceAddressPrefix: jumpboxSubnetPrefix
      sourcePortRange: '*'
      destinationAddressPrefix: 'Internet'
      destinationPortRanges: ['80', '443']
    }
  }
]

module bastionNsg '../network/nsg.bicep' = {
  name: 'private-nsg-bastion'
  params: {
    name: 'nsg-${namePrefix}-private-bastion'
    location: location
    tags: tags
    securityRules: bastionNsgRules
    enableDenyAllBaseline: true
    logAnalyticsWorkspaceId: logAnalyticsWorkspaceId
  }
}

module privateEndpointNsg '../network/nsg.bicep' = {
  name: 'private-nsg-pe'
  params: {
    name: 'nsg-${namePrefix}-private-pe'
    location: location
    tags: tags
    securityRules: privateEndpointNsgRules
    enableDenyAllBaseline: true
    logAnalyticsWorkspaceId: logAnalyticsWorkspaceId
  }
}

module jumpboxNsg '../network/nsg.bicep' = {
  name: 'private-nsg-jumpbox'
  params: {
    name: 'nsg-${namePrefix}-private-jumpbox'
    location: location
    tags: tags
    securityRules: jumpboxNsgRules
    enableDenyAllBaseline: true
    logAnalyticsWorkspaceId: logAnalyticsWorkspaceId
  }
}

// ---------------------------------------------------------------------------
// Route Table - 방화벽보다 먼저 만들어 순환 의존을 끊는다
// ---------------------------------------------------------------------------

module jumpboxRouteTable '../network/route-table.bicep' = {
  name: 'private-route-table-jumpbox'
  params: {
    name: 'rt-${namePrefix}-private-jumpbox'
    location: location
    tags: tags
    firewallPrivateIpAddress: firewallPrivateIpAddress
    disableBgpRoutePropagation: true
  }
}

// ---------------------------------------------------------------------------
// VNet
// ---------------------------------------------------------------------------

// AzureFirewallSubnet / AzureFirewallManagementSubnet은 NSG 연결이 플랫폼 차원에서
// 불가능하므로 networkSecurityGroupId에 빈 문자열을 넘긴다. 유일한 예외다.
var firewallSubnets subnetConfig[] = concat(
  [
    {
      name: firewallSubnetName
      addressPrefix: firewallSubnetPrefix
      networkSecurityGroupId: ''
    }
  ],
  requiresFirewallManagementSubnet
    ? [
        {
          name: firewallManagementSubnetName
          addressPrefix: firewallManagementSubnetPrefix
          networkSecurityGroupId: ''
        }
      ]
    : []
)

var managedSubnets subnetConfig[] = [
  {
    name: bastionSubnetName
    addressPrefix: bastionSubnetPrefix
    networkSecurityGroupId: bastionNsg.outputs.id
    // Bastion 서브넷에는 0.0.0.0/0 UDR을 적용하면 안 된다(제어 평면이 끊긴다).
  }
  {
    name: privateEndpointSubnetName
    addressPrefix: privateEndpointSubnetPrefix
    networkSecurityGroupId: privateEndpointNsg.outputs.id
    privateEndpointNetworkPolicies: privateEndpointNetworkPolicies
  }
  {
    name: jumpboxSubnetName
    addressPrefix: jumpboxSubnetPrefix
    networkSecurityGroupId: jumpboxNsg.outputs.id
    routeTableId: jumpboxRouteTable.outputs.id
  }
]

module vnet '../network/vnet.bicep' = {
  name: 'private-vnet'
  params: {
    name: 'vnet-${namePrefix}-private'
    location: location
    tags: tags
    addressPrefixes: [vnetAddressPrefix]
    subnets: concat(firewallSubnets, managedSubnets)
  }
}

// ---------------------------------------------------------------------------
// Azure Firewall - L7 FQDN 화이트리스트
// ---------------------------------------------------------------------------

module firewallPolicy '../network/firewall-policy.bicep' = {
  name: 'private-firewall-policy'
  params: {
    name: 'afwp-${namePrefix}-private'
    location: location
    tags: tags
    skuTier: firewallSkuTier
    sourceAddresses: [jumpboxSubnetPrefix]
    additionalAllowedFqdns: additionalAllowedFqdns
  }
}

module firewall '../network/firewall.bicep' = {
  name: 'private-firewall'
  params: {
    name: 'afw-${namePrefix}-private'
    location: location
    tags: tags
    skuTier: firewallSkuTier
    subnetId: vnet.outputs.subnetIds[firewallSubnetName]
    managementSubnetId: requiresFirewallManagementSubnet
      ? vnet.outputs.subnetIds[firewallManagementSubnetName]
      : ''
    firewallPolicyId: firewallPolicy.outputs.id
    logAnalyticsWorkspaceId: logAnalyticsWorkspaceId
  }
}

// ---------------------------------------------------------------------------
// Azure Bastion
// ---------------------------------------------------------------------------

module bastion '../network/bastion.bicep' = {
  name: 'private-bastion'
  params: {
    name: 'bas-${namePrefix}-private'
    location: location
    tags: tags
    skuName: bastionSkuName
    subnetId: vnet.outputs.subnetIds[bastionSubnetName]
    logAnalyticsWorkspaceId: logAnalyticsWorkspaceId
  }
}

// ---------------------------------------------------------------------------
// Private DNS
// ---------------------------------------------------------------------------

module privateDnsZones '../network/private-dns-zone.bicep' = [
  for zoneName in privateDnsZoneNames: {
    name: 'private-dns-${replace(zoneName, '.', '-')}'
    params: {
      name: zoneName
      tags: tags
      virtualNetworkId: vnet.outputs.id
    }
  }
]

// ---------------------------------------------------------------------------
// Azure AI Foundry (완전 비공개)
// ---------------------------------------------------------------------------

var foundryAccountName = 'aif-${namePrefix}-prv-${resourceToken}'

module foundry '../ai/foundry-account.bicep' = {
  name: 'private-foundry-account'
  params: {
    name: foundryAccountName
    location: location
    tags: tags
    customSubDomainName: foundryAccountName
    publicNetworkAccess: 'Disabled'
    disableLocalAuth: true
    allowedIpAddresses: []
    allowedSubnetIds: []
    // 신뢰할 수 있는 Azure 서비스도 우회 불가. Private Endpoint만이 유일한 접근 경로다.
    networkAclsBypass: 'None'
    logAnalyticsWorkspaceId: logAnalyticsWorkspaceId
  }
}

module foundryPrivateEndpoint '../network/private-endpoint.bicep' = {
  name: 'private-foundry-pe'
  params: {
    name: 'pe-${foundryAccountName}'
    location: location
    tags: tags
    subnetId: vnet.outputs.subnetIds[privateEndpointSubnetName]
    targetResourceId: foundry.outputs.id
    groupIds: ['account']
    privateDnsZoneIds: [
      for (zoneName, index) in privateDnsZoneNames: privateDnsZones[index].outputs.id
    ]
  }
}

module project '../ai/foundry-project.bicep' = {
  name: 'private-foundry-project'
  params: {
    accountName: foundry.outputs.name
    name: projectName
    location: location
    tags: tags
    displayName: 'Private 망 실습 프로젝트'
    projectDescription: 'Private Endpoint 경유로만 접근 가능한 Foundry 프로젝트'
  }
}

module deployments '../ai/model-deployments.bicep' = {
  name: 'private-foundry-deployments'
  params: {
    accountName: foundry.outputs.name
    deployments: modelDeployments
  }
  dependsOn: [
    project
  ]
}

// ---------------------------------------------------------------------------
// 점프박스
// ---------------------------------------------------------------------------

module jumpbox '../compute/jumpbox.bicep' = {
  name: 'private-jumpbox'
  params: {
    // Windows 컴퓨터 이름은 15자를 넘을 수 없다.
    name: take('vm-jb-${resourceToken}', 15)
    location: location
    tags: tags
    subnetId: vnet.outputs.subnetIds[jumpboxSubnetName]
    vmSize: vmSize
    adminUsername: vmAdminUsername
    adminPassword: vmAdminPassword
  }
}

// ---------------------------------------------------------------------------
// RBAC - keyless 접근의 유일한 통로
// ---------------------------------------------------------------------------

var labUserAssignments roleAssignmentConfig[] = empty(labUserPrincipalId) ? [] : [
  {
    principalId: labUserPrincipalId
    principalType: labUserPrincipalType
    roleDefinitionId: COGNITIVE_SERVICES_OPENAI_USER_ROLE_ID
    description: '실습자 - OpenAI 모델 추론 호출 (keyless)'
  }
  {
    principalId: labUserPrincipalId
    principalType: labUserPrincipalType
    roleDefinitionId: COGNITIVE_SERVICES_USER_ROLE_ID
    description: '실습자 - AI Services 데이터 평면 호출 (keyless)'
  }
  {
    principalId: labUserPrincipalId
    principalType: labUserPrincipalType
    roleDefinitionId: AZURE_AI_DEVELOPER_ROLE_ID
    description: '실습자 - Foundry 프로젝트 작업'
  }
]

var jumpboxAssignments roleAssignmentConfig[] = [
  {
    principalId: jumpbox.outputs.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: COGNITIVE_SERVICES_OPENAI_USER_ROLE_ID
    description: '점프박스 관리 ID - OpenAI 모델 추론 호출 (keyless)'
  }
  {
    principalId: jumpbox.outputs.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: COGNITIVE_SERVICES_USER_ROLE_ID
    description: '점프박스 관리 ID - AI Services 데이터 평면 호출 (keyless)'
  }
]

module roleAssignments '../identity/foundry-role-assignments.bicep' = {
  name: 'private-foundry-roles'
  params: {
    accountName: foundry.outputs.name
    assignments: concat(labUserAssignments, jumpboxAssignments)
  }
}

// ---------------------------------------------------------------------------
// 출력
// ---------------------------------------------------------------------------

@description('Private VNet 리소스 ID')
output vnetId string = vnet.outputs.id

@description('NSG가 연결되지 않은 서브넷 목록. NSG를 지원하지 않는 방화벽 서브넷만 나와야 한다.')
output subnetsWithoutNsg string[] = vnet.outputs.subnetsWithoutNsg

@description('Azure Firewall 이름')
output firewallName string = firewall.outputs.name

@description('UDR에 설정한 방화벽 IP (배포 전 계산값)')
output routeNextHopIpAddress string = firewallPrivateIpAddress

@description('Azure Firewall의 실제 사설 IP')
output firewallPrivateIpAddress string = firewall.outputs.privateIpAddress

@description('계산한 UDR next hop이 실제 방화벽 IP와 일치하는지 여부. false면 강제 터널링이 끊긴 상태다.')
output firewallPrivateIpMatchesRoute bool = firewall.outputs.privateIpAddress == firewallPrivateIpAddress

@description('방화벽 아웃바운드 공인 IP')
output firewallPublicIpAddress string = firewall.outputs.publicIpAddress

@description('Azure Bastion 이름')
output bastionName string = bastion.outputs.name

@description('점프박스 VM 이름')
output jumpboxName string = jumpbox.outputs.name

@description('점프박스 사설 IP')
output jumpboxPrivateIpAddress string = jumpbox.outputs.privateIpAddress

@description('Private Foundry 계정 이름')
output foundryAccountName string = foundry.outputs.name

@description('Private Foundry 엔드포인트. Private Endpoint 경유로만 해석된다.')
output foundryEndpoint string = foundry.outputs.endpoint

@description('Private Foundry 프로젝트 이름')
output foundryProjectName string = project.outputs.name

@description('Foundry Private Endpoint 이름')
output foundryPrivateEndpointName string = foundryPrivateEndpoint.outputs.name
