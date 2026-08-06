metadata description = '''
Private 망 공통 리소스 묶음 (리소스 그룹 범위에 배포되는 모듈).

여러 리소스를 한 번에 만드는 composite module 이다. 만드는 리소스는 다음과 같다.
  - Virtual Network (VNet)
  - Network Security Group (NSG) 3~4개와 서브넷 연결
  - Azure Bastion
  - 점프박스 역할을 하는 Windows 가상 머신(VM)
  - Azure AI Foundry 계정 + Private Endpoint + Private DNS Zone
  - Foundry 호출에 필요한 RBAC 역할 할당 (API 키를 쓰지 않는 keyless 인증)
  - (선택) Azure Machine Learning 묶음 — machineLearningSubnetPrefix 를 준 경우에만.
    전용 서브넷 snet-aml 과 그 NSG 를 만들고 machine-learning.bicep 을 호출한다.

private 시스템과 private-whitelist 시스템이 이 모듈을 공통으로 사용한다.
두 시스템이 같은 내용을 각자 복사해서 갖고 있으면, 한쪽만 수정했을 때 보안 설정이
서로 어긋나는 문제(configuration drift)가 생긴다. 그래서 공통 부분은 이 파일에만 둔다.

이 모듈이 적용하는 접근 통제는 두 가지다.
  1) NSG: 모든 서브넷에 NSG를 연결하고, 우선순위 4096에 모든 트래픽을 차단하는
     기본 규칙(deny-all)을 깔아 둔다. 그 위에 필요한 통신만 개별 규칙으로 허용한다.
  2) Foundry 서비스 자체의 네트워크 설정: publicNetworkAccess=Disabled 로 공용 엔드포인트를
     닫고, networkAcls.bypass=None 으로 "신뢰할 수 있는 Azure 서비스"의 예외 통과도 막는다.
     결과적으로 Private Endpoint 를 통한 접근만 가능하다.

private 시스템과 private-whitelist 시스템의 차이는 아래 매개변수 3개로만 표현된다.
  platformSubnets             : NSG를 연결할 수 없는 서브넷(AzureFirewallSubnet 등)을 추가로 만든다.
                                private 시스템은 빈 배열을 넘기고,
                                private-whitelist 시스템은 방화벽용 서브넷을 넘긴다.
  jumpboxRouteTableId         : 점프박스 서브넷에 연결할 Route Table 의 리소스 ID.
                                private 시스템은 빈 문자열을 넘겨 인터넷으로 바로 나가게 하고,
                                private-whitelist 시스템은 "0.0.0.0/0 -> 방화벽" 경로를 넘긴다.
  machineLearningSubnetPrefix : Azure Machine Learning 전용 서브넷의 CIDR.
                                private 시스템은 CIDR 을 넘겨 AML 을 함께 배포하고,
                                private-whitelist 시스템은 빈 문자열을 넘겨 배포하지 않는다.

NSG 규칙은 IP 대역과 포트(OSI 3~4계층) 수준까지만 허용 범위를 정한다.
도메인 이름(FQDN) 단위의 통제는 이 모듈이 아니라 Azure Firewall 이 담당한다.
'''

import { subnetConfig } from '../network/vnet.bicep'
import { modelDeploymentConfig } from '../ai/model-deployments.bicep'
import { roleAssignmentConfig } from '../identity/foundry-role-assignments.bicep'
import {
  COGNITIVE_SERVICES_USER_ROLE_ID
  COGNITIVE_SERVICES_OPENAI_USER_ROLE_ID
  AZURE_AI_DEVELOPER_ROLE_ID
} from '../identity/role-definitions.bicep'

@description('리소스 이름 접두사. 소문자여야 한다(Foundry 엔드포인트가 DNS 이름이다).')
param namePrefix string

@description('리소스 이름 접미사. 시스템을 구분한다. 예: private, private-whitelist')
param nameSuffix string

@description('''
Foundry 계정 이름에 쓰는 짧은 시스템 약어. 예: priv, privwl

Foundry 계정 이름은 64자를 넘을 수 없고 전역 고유 DNS 이름이기도 하다.
nameSuffix(private-whitelist)를 그대로 쓰면 리전 이름까지 붙였을 때 한계를 넘으므로
계정 이름에서만 약어를 쓴다. 나머지 리소스는 nameSuffix를 그대로 쓴다.
''')
param systemAbbrev string

@description('전역 고유 이름에 사용할 토큰')
param resourceToken string

@description('배포 리전')
param location string

@description('리소스 태그')
param tags object = {}

@description('VNet 주소 공간')
param vnetAddressPrefix string

@description('''
NSG를 연결할 수 없는 플랫폼 서브넷 목록. VNet에 그대로 추가된다.
networkSecurityGroupId를 빈 문자열로 넘긴 항목은 subnetsWithoutNsg 출력에 드러난다.
''')
param platformSubnets subnetConfig[] = []

@description('AzureBastionSubnet CIDR. /26 이상이어야 한다.')
param bastionSubnetPrefix string

@description('Private Endpoint 서브넷 CIDR')
param privateEndpointSubnetPrefix string

@description('점프박스 서브넷 CIDR')
param jumpboxSubnetPrefix string

@description('''
Azure Machine Learning 전용 서브넷 CIDR. 빈 문자열이면 AML 을 배포하지 않는다.

값을 주면 snet-aml 서브넷과 전용 NSG 를 만들고, 그 서브넷에 AML 워크스페이스와
연결 리소스(스토리지 · Key Vault)의 Private Endpoint 를 놓는다.
자세한 구성은 modules/workload/machine-learning.bicep 을 참고한다.
''')
param machineLearningSubnetPrefix string = ''

@description('AML 워크스페이스의 관리형 네트워크 격리 모드. 컴퓨팅이 놓일 네트워크를 결정한다.')
@allowed(['Disabled', 'AllowInternetOutbound', 'AllowOnlyApprovedOutbound'])
param machineLearningIsolationMode string = 'AllowInternetOutbound'

@description('점프박스 서브넷에 연결할 Route Table 리소스 ID. 빈 문자열이면 UDR 없이 인터넷 직통이다.')
param jumpboxRouteTableId string = ''

@description('Azure Bastion SKU')
@allowed(['Basic', 'Standard'])
param bastionSkuName string = 'Basic'

@description('Private Endpoint 서브넷에 NSG를 적용할지 여부')
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

@description('''
Private DNS Zone을 VNet에 연결할지 여부. 기본값은 false다.

false면 점프박스가 on-prem 노트북처럼 동작한다. Private DNS Zone을 볼 수 없어
Foundry 이름이 공용 IP로 해석되고, 모든 트래픽이 방화벽을 거쳐 로그에 FQDN이 남는다.
고객사 방화벽에 제출할 FQDN 목록을 빠짐없이 뽑는 것이 목적이다.

true면 사설 IP로 해석되어 Private Endpoint 경유로 접근한다.
''')
param linkPrivateDnsZonesToVnet bool = false

@description('Foundry 프로젝트 이름')
param projectName string

@description('Foundry 프로젝트 표시 이름')
param projectDisplayName string

@description('Foundry 프로젝트 설명')
param projectDescription string

@description('AML 워크스페이스 표시 이름. machineLearningSubnetPrefix 를 준 경우에만 쓰인다.')
param machineLearningFriendlyName string = 'Private 망 실습 워크스페이스'

@description('AML 워크스페이스 설명. machineLearningSubnetPrefix 를 준 경우에만 쓰인다.')
param machineLearningDescription string = 'Private Endpoint 경유로만 접근 가능한 Machine Learning 워크스페이스'

@description('배포할 모델 목록')
param modelDeployments modelDeploymentConfig[]

@description('진단 로그를 보낼 Log Analytics 작업 영역 ID')
param logAnalyticsWorkspaceId string = ''

// ---------------------------------------------------------------------------
// 상수
// ---------------------------------------------------------------------------

// Azure 플랫폼 DNS. VNet에 링크된 Private DNS Zone도 이 주소를 통해 조회된다.
var AZURE_PLATFORM_DNS_IP = '168.63.129.16'

var privateEndpointSubnetName = 'snet-private-endpoint'
var jumpboxSubnetName = 'snet-jumpbox'
var bastionSubnetName = 'AzureBastionSubnet'
var machineLearningSubnetName = 'snet-aml'

// AML 은 서브넷 CIDR 을 준 시스템에서만 배포된다. private 은 주고, private-whitelist 는 주지 않는다.
var deployMachineLearning = !empty(machineLearningSubnetPrefix)

var stackName = '${namePrefix}-${nameSuffix}'

// Foundry Private Endpoint 이름 해석에 필요한 존. AI Foundry는 세 개 모두 필요하다.
var privateDnsZoneNames = [
  'privatelink.cognitiveservices.azure.com'
  'privatelink.openai.azure.com'
  'privatelink.services.ai.azure.com'
]

// ---------------------------------------------------------------------------
// NSG - 모든 서브넷에 연결한다. 기본은 전부 차단(deny-all)이고, 필요한 통신만 규칙으로 허용한다.
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

// NSG 규칙은 라우팅으로 결정된 다음 홉(next hop)이 아니라, 패킷에 적힌 원래 목적지를 기준으로 평가된다.
// 따라서 Route Table 로 방화벽에 보내는 트래픽이라도 NSG에서는 원래 목적지(Internet 등)를 허용해야 통과한다.
// 여기서는 포트 수준까지만 허용하고, 어떤 도메인으로 나갈 수 있는지는 Azure Firewall 이 통제한다.
var jumpboxBaseNsgRules = [
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
    name: 'AllowInternetOutbound'
    properties: {
      description: 'HTTP/HTTPS 아웃바운드. UDR이 걸린 시스템에서는 방화벽으로 강제 터널링되어 FQDN 통제를 받는다.'
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

// AML 을 배포하는 시스템에서만 붙는 규칙.
// 스튜디오·SDK 호출도, 데이터스토어 읽기·쓰기도 모두 snet-aml 의 Private Endpoint 를 향한다.
var jumpboxMachineLearningNsgRules = deployMachineLearning ? [
  {
    name: 'AllowMachineLearningOutbound'
    properties: {
      description: 'Private Endpoint 경유 AML 워크스페이스 / 스토리지 / Key Vault 호출.'
      priority: 130
      direction: 'Outbound'
      access: 'Allow'
      protocol: 'Tcp'
      sourceAddressPrefix: jumpboxSubnetPrefix
      sourcePortRange: '*'
      destinationAddressPrefix: machineLearningSubnetPrefix
      destinationPortRange: '443'
    }
  }
] : []

var jumpboxNsgRules = concat(jumpboxBaseNsgRules, jumpboxMachineLearningNsgRules)

// AML 서브넷에는 Private Endpoint 만 놓인다. 들어오는 트래픽은 점프박스의 HTTPS 뿐이고,
// Private Endpoint 는 스스로 아웃바운드를 만들지 않으므로 아웃바운드는 전부 차단된 상태로 둔다.
var machineLearningNsgRules = [
  {
    name: 'AllowJumpboxHttpsInbound'
    properties: {
      description: '점프박스 서브넷에서 AML Private Endpoint 로의 HTTPS 만 허용.'
      priority: 100
      direction: 'Inbound'
      access: 'Allow'
      protocol: 'Tcp'
      sourceAddressPrefix: jumpboxSubnetPrefix
      sourcePortRange: '*'
      destinationAddressPrefix: machineLearningSubnetPrefix
      destinationPortRange: '443'
    }
  }
]

module bastionNsg '../network/nsg.bicep' = {
  name: 'nsg-bastion-${nameSuffix}'
  params: {
    name: 'nsg-${stackName}-bastion'
    location: location
    tags: tags
    securityRules: bastionNsgRules
    enableDenyAllBaseline: true
    logAnalyticsWorkspaceId: logAnalyticsWorkspaceId
  }
}

module privateEndpointNsg '../network/nsg.bicep' = {
  name: 'nsg-pe-${nameSuffix}'
  params: {
    name: 'nsg-${stackName}-pe'
    location: location
    tags: tags
    securityRules: privateEndpointNsgRules
    enableDenyAllBaseline: true
    logAnalyticsWorkspaceId: logAnalyticsWorkspaceId
  }
}

module jumpboxNsg '../network/nsg.bicep' = {
  name: 'nsg-jumpbox-${nameSuffix}'
  params: {
    name: 'nsg-${stackName}-jumpbox'
    location: location
    tags: tags
    securityRules: jumpboxNsgRules
    enableDenyAllBaseline: true
    logAnalyticsWorkspaceId: logAnalyticsWorkspaceId
  }
}

module machineLearningNsg '../network/nsg.bicep' = if (deployMachineLearning) {
  name: 'nsg-aml-${nameSuffix}'
  params: {
    name: 'nsg-${stackName}-aml'
    location: location
    tags: tags
    securityRules: machineLearningNsgRules
    enableDenyAllBaseline: true
    logAnalyticsWorkspaceId: logAnalyticsWorkspaceId
  }
}

// ---------------------------------------------------------------------------
// VNet
// ---------------------------------------------------------------------------

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
    routeTableId: jumpboxRouteTableId
  }
]

// AML 을 배포하지 않는 시스템에서는 빈 배열이 되어 서브넷 자체가 만들어지지 않는다.
var machineLearningSubnets subnetConfig[] = deployMachineLearning ? [
  {
    name: machineLearningSubnetName
    addressPrefix: machineLearningSubnetPrefix
    networkSecurityGroupId: machineLearningNsg!.outputs.id
    privateEndpointNetworkPolicies: privateEndpointNetworkPolicies
  }
] : []

module vnet '../network/vnet.bicep' = {
  name: 'vnet-${nameSuffix}'
  params: {
    name: 'vnet-${stackName}'
    location: location
    tags: tags
    addressPrefixes: [vnetAddressPrefix]
    subnets: concat(platformSubnets, managedSubnets, machineLearningSubnets)
  }
}

// ---------------------------------------------------------------------------
// Azure Bastion
// ---------------------------------------------------------------------------

module bastion '../network/bastion.bicep' = {
  name: 'bastion-${nameSuffix}'
  params: {
    name: 'bas-${stackName}'
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
    name: 'dns-${nameSuffix}-${replace(zoneName, '.', '-')}'
    params: {
      name: zoneName
      tags: tags
      virtualNetworkId: vnet.outputs.id
      linkToVirtualNetwork: linkPrivateDnsZonesToVnet
    }
  }
]

// ---------------------------------------------------------------------------
// Azure AI Foundry (완전 비공개)
// ---------------------------------------------------------------------------

// 계정 이름에 리전을 넣어, 같은 리소스 그룹에 여러 리전의 Foundry를 둘 수 있게 한다.
// 최대 길이: aif-(4) + namePrefix(16) + -(1) + systemAbbrev(6) + -(1) + location(18) + -(1) + token(13) = 60자
var foundryAccountName = 'aif-${namePrefix}-${systemAbbrev}-${location}-${resourceToken}'

module foundry '../ai/foundry-account.bicep' = {
  name: 'foundry-account-${nameSuffix}'
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
  name: 'foundry-pe-${nameSuffix}'
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
  name: 'foundry-project-${nameSuffix}'
  params: {
    accountName: foundry.outputs.name
    name: projectName
    location: location
    tags: tags
    displayName: projectDisplayName
    projectDescription: projectDescription
  }
}

module deployments '../ai/model-deployments.bicep' = {
  name: 'foundry-deployments-${nameSuffix}'
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
  name: 'jumpbox-${nameSuffix}'
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
// Azure Machine Learning (선택)
//
// 워크스페이스와 연결 리소스는 Foundry 와 같은 VNet 의 다른 서브넷(snet-aml)에 붙는다.
// Foundry 용 Private Endpoint 서브넷과 분리해 두면, NSG 로 두 서비스의 접근 경로를
// 따로 통제할 수 있고 어떤 트래픽이 어디로 가는지도 서브넷 단위로 드러난다.
// ---------------------------------------------------------------------------

module machineLearning './machine-learning.bicep' = if (deployMachineLearning) {
  name: 'machine-learning-${nameSuffix}'
  params: {
    namePrefix: namePrefix
    nameSuffix: nameSuffix
    resourceToken: resourceToken
    location: location
    tags: tags
    virtualNetworkId: vnet.outputs.id
    subnetId: vnet.outputs.subnetIds[machineLearningSubnetName]
    linkPrivateDnsZonesToVnet: linkPrivateDnsZonesToVnet
    managedNetworkIsolationMode: machineLearningIsolationMode
    workspaceFriendlyName: machineLearningFriendlyName
    workspaceDescription: machineLearningDescription
    labUserPrincipalId: labUserPrincipalId
    labUserPrincipalType: labUserPrincipalType
    jumpboxPrincipalId: jumpbox.outputs.principalId
    logAnalyticsWorkspaceId: logAnalyticsWorkspaceId
  }
}

// ---------------------------------------------------------------------------
// RBAC - API 키를 쓰지 않으므로, Foundry 호출 권한은 역할 할당으로만 부여된다
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
  name: 'foundry-roles-${nameSuffix}'
  params: {
    accountName: foundry.outputs.name
    assignments: concat(labUserAssignments, jumpboxAssignments)
  }
}

// ---------------------------------------------------------------------------
// 출력
// ---------------------------------------------------------------------------

@description('VNet 리소스 ID')
output vnetId string = vnet.outputs.id

@description('VNet 이름')
output vnetName string = vnet.outputs.name

@description('서브넷 이름 -> 리소스 ID 매핑')
output subnetIds object = vnet.outputs.subnetIds

@description('NSG가 연결되지 않은 서브넷 목록. platformSubnets로 넘긴 항목만 나와야 한다.')
output subnetsWithoutNsg string[] = vnet.outputs.subnetsWithoutNsg

@description('Azure Bastion 이름')
output bastionName string = bastion.outputs.name

@description('점프박스 VM 이름')
output jumpboxName string = jumpbox.outputs.name

@description('점프박스 사설 IP')
output jumpboxPrivateIpAddress string = jumpbox.outputs.privateIpAddress

@description('Foundry 계정 이름')
output foundryAccountName string = foundry.outputs.name

@description('Foundry 엔드포인트. Private Endpoint 경유로만 해석된다.')
output foundryEndpoint string = foundry.outputs.endpoint

@description('Foundry 프로젝트 이름')
output foundryProjectName string = project.outputs.name

@description('Foundry Private Endpoint 이름')
output foundryPrivateEndpointName string = foundryPrivateEndpoint.outputs.name

@description('AML 을 배포했는지 여부')
output machineLearningDeployed bool = deployMachineLearning

@description('AML 워크스페이스 이름. 배포하지 않았으면 빈 문자열.')
output machineLearningWorkspaceName string = deployMachineLearning ? machineLearning!.outputs.workspaceName : ''

@description('AML 기본 데이터스토어 스토리지 계정 이름. 배포하지 않았으면 빈 문자열.')
output machineLearningStorageAccountName string = deployMachineLearning ? machineLearning!.outputs.storageAccountName : ''

@description('AML Key Vault 이름. 배포하지 않았으면 빈 문자열.')
output machineLearningKeyVaultName string = deployMachineLearning ? machineLearning!.outputs.keyVaultName : ''
