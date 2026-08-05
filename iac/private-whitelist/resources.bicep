metadata description = '''
Private 망 + 접속 허용 도메인 목록 리소스 (리소스 그룹 범위에 배포된다).

공통 모듈 modules/workload/private-foundry-workload.bicep 이 만드는 리소스에 세 가지를 추가한다.
  1) 방화벽용 서브넷(AzureFirewallSubnet, AzureFirewallManagementSubnet)
     — VNet 정의에 함께 넣는다. 이 두 서브넷은 Azure 플랫폼 제약으로 NSG를 연결할 수 없어
       networkSecurityGroupId 에 빈 문자열을 넘기며, subnetsWithoutNsg 출력에 표시된다.
  2) Route Table — 점프박스 서브넷에서 나가는 모든 트래픽(0.0.0.0/0)을 방화벽으로 보낸다.
  3) Firewall Policy 와 Azure Firewall — 점프박스가 접속할 수 있는 도메인을 목록으로 제한한다.

배포 순서에 문제가 없는 이유
  Route Table 의 다음 홉(next hop) 주소는 방화벽이 할당받을 IP를 cidrHost 함수로 미리 계산한 값이다.
  그래서 VNet -> Firewall -> Route Table -> VNet 으로 이어지는 순환 참조 없이
  Route Table -> VNet -> Firewall 순서로 한 시스템 안에서 모두 배포된다.
  Firewall Policy 와 Azure Firewall 도 같은 파일에 둔다.
  Azure Firewall 은 생성 시점에 연결할 정책이 이미 있어야 하기 때문에, 둘을 다른 시스템으로 나누면
  배포 순서를 사람이 맞춰야 한다.
'''

import { subnetConfig } from '../modules/network/vnet.bicep'
import { modelDeploymentConfig } from '../modules/ai/model-deployments.bicep'

@description('리소스 이름 접두사')
param namePrefix string

@description('리소스 이름 접미사. 시스템을 구분한다.')
param nameSuffix string

@description('전역 고유 이름에 사용할 토큰')
param resourceToken string

@description('배포 리전')
param location string

@description('리소스 태그')
param tags object = {}

@description('VNet 주소 공간')
param vnetAddressPrefix string

@description('AzureFirewallSubnet CIDR. /26 이상이어야 한다.')
param firewallSubnetPrefix string

@description('AzureFirewallManagementSubnet CIDR')
param firewallManagementSubnetPrefix string

@description('AzureFirewallManagementSubnet을 만들지 여부. Azure Firewall Basic SKU에서 필수다.')
param deployFirewallManagementSubnet bool = true

@description('AzureBastionSubnet CIDR. /26 이상이어야 한다.')
param bastionSubnetPrefix string

@description('Private Endpoint 서브넷 CIDR')
param privateEndpointSubnetPrefix string

@description('점프박스 서브넷 CIDR')
param jumpboxSubnetPrefix string

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

@description('배포할 모델 목록')
param modelDeployments modelDeploymentConfig[]

@description('Azure Firewall SKU')
@allowed(['Basic', 'Standard', 'Premium'])
param firewallSkuTier string = 'Basic'

@description('위협 인텔리전스 모드')
@allowed(['Off', 'Alert', 'Deny'])
param threatIntelMode string = 'Alert'

@description('L4로 허용할 Azure 서비스 태그 목록')
param allowedServiceTags string[]

@description('Entra ID 인증 및 Azure 관리 평면 FQDN 화이트리스트')
param identityAndManagementFqdns string[]

@description('Azure AI Foundry FQDN 화이트리스트')
param foundryFqdns string[]

@description('Azure Portal FQDN 화이트리스트')
param portalFqdns string[]

@description('실습 도구 FQDN 화이트리스트')
param toolingFqdns string[]

@description('추가 FQDN 화이트리스트')
param additionalAllowedFqdns string[] = []

@description('Azure Firewall FQDN 태그 화이트리스트')
param allowedFqdnTags string[]

@description('진단 로그를 보낼 Log Analytics 작업 영역 ID')
param logAnalyticsWorkspaceId string = ''

// ---------------------------------------------------------------------------
// 상수
// ---------------------------------------------------------------------------

// Azure Firewall이 전용 서브넷에서 받는 첫 할당 가능 주소의 인덱스(.0 .1 .2 .3 예약 이후).
var FIREWALL_HOST_INDEX = 3

var firewallSubnetName = 'AzureFirewallSubnet'
var firewallManagementSubnetName = 'AzureFirewallManagementSubnet'

var stackName = '${namePrefix}-${nameSuffix}'
var firewallPrivateIpAddress = cidrHost(firewallSubnetPrefix, FIREWALL_HOST_INDEX)

// ---------------------------------------------------------------------------
// Route Table - 방화벽보다 먼저 만들 수 있게 next hop을 계산으로 구한다
// ---------------------------------------------------------------------------

module jumpboxRouteTable '../modules/network/route-table.bicep' = {
  name: 'route-table-jumpbox-${nameSuffix}'
  params: {
    name: 'rt-${stackName}-jumpbox'
    location: location
    tags: tags
    firewallPrivateIpAddress: firewallPrivateIpAddress
    disableBgpRoutePropagation: true
  }
}

// ---------------------------------------------------------------------------
// 공통 모듈 - VNet / NSG / Bastion / 점프박스 VM / Foundry(Private Endpoint, DNS) / 역할 할당
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
  deployFirewallManagementSubnet
    ? [
        {
          name: firewallManagementSubnetName
          addressPrefix: firewallManagementSubnetPrefix
          networkSecurityGroupId: ''
        }
      ]
    : []
)

module workload '../modules/workload/private-foundry-workload.bicep' = {
  name: 'workload-${nameSuffix}'
  params: {
    namePrefix: namePrefix
    nameSuffix: nameSuffix
    resourceToken: resourceToken
    location: location
    tags: tags
    vnetAddressPrefix: vnetAddressPrefix
    platformSubnets: firewallSubnets
    bastionSubnetPrefix: bastionSubnetPrefix
    privateEndpointSubnetPrefix: privateEndpointSubnetPrefix
    jumpboxSubnetPrefix: jumpboxSubnetPrefix
    jumpboxRouteTableId: jumpboxRouteTable.outputs.id
    bastionSkuName: bastionSkuName
    privateEndpointNetworkPolicies: privateEndpointNetworkPolicies
    vmSize: vmSize
    vmAdminUsername: vmAdminUsername
    vmAdminPassword: vmAdminPassword
    labUserPrincipalId: labUserPrincipalId
    labUserPrincipalType: labUserPrincipalType
    projectName: 'proj-private-whitelist'
    projectDisplayName: 'Private 망 + 접속 허용 도메인 제한 실습 프로젝트'
    projectDescription: 'Private Endpoint 로만 접근할 수 있고, 아웃바운드는 방화벽이 허용한 도메인으로만 나갈 수 있는 Foundry 프로젝트'
    modelDeployments: modelDeployments
    logAnalyticsWorkspaceId: logAnalyticsWorkspaceId
  }
}

// ---------------------------------------------------------------------------
// Azure Firewall - 점프박스가 접속할 수 있는 도메인을 여기서 제한한다
// ---------------------------------------------------------------------------

module firewallPolicy '../modules/network/firewall-policy.bicep' = {
  name: 'firewall-policy-${nameSuffix}'
  params: {
    name: 'afwp-${stackName}'
    location: location
    tags: tags
    skuTier: firewallSkuTier
    sourceAddresses: [jumpboxSubnetPrefix]
    allowedServiceTags: allowedServiceTags
    identityAndManagementFqdns: identityAndManagementFqdns
    foundryFqdns: foundryFqdns
    portalFqdns: portalFqdns
    toolingFqdns: toolingFqdns
    additionalAllowedFqdns: additionalAllowedFqdns
    allowedFqdnTags: allowedFqdnTags
    threatIntelMode: threatIntelMode
  }
}

module firewall '../modules/network/firewall.bicep' = {
  name: 'firewall-${nameSuffix}'
  params: {
    name: 'afw-${stackName}'
    location: location
    tags: tags
    skuTier: firewallSkuTier
    subnetId: workload.outputs.subnetIds[firewallSubnetName]
    managementSubnetId: deployFirewallManagementSubnet
      ? workload.outputs.subnetIds[firewallManagementSubnetName]
      : ''
    firewallPolicyId: firewallPolicy.outputs.id
    logAnalyticsWorkspaceId: logAnalyticsWorkspaceId
  }
}

// ---------------------------------------------------------------------------
// 출력
// ---------------------------------------------------------------------------

@description('VNet 리소스 ID')
output vnetId string = workload.outputs.vnetId

@description('VNet 이름')
output vnetName string = workload.outputs.vnetName

@description('NSG가 연결되지 않은 서브넷 목록. 방화벽 서브넷만 나와야 한다.')
output subnetsWithoutNsg string[] = workload.outputs.subnetsWithoutNsg

@description('Route Table에 설정한 방화벽 IP(계산값)')
output routeNextHopIpAddress string = firewallPrivateIpAddress

@description('Azure Bastion 이름')
output bastionName string = workload.outputs.bastionName

@description('점프박스 VM 이름')
output jumpboxName string = workload.outputs.jumpboxName

@description('점프박스 사설 IP')
output jumpboxPrivateIpAddress string = workload.outputs.jumpboxPrivateIpAddress

@description('Foundry 계정 이름')
output foundryAccountName string = workload.outputs.foundryAccountName

@description('Foundry 엔드포인트. Private Endpoint 경유로만 해석된다.')
output foundryEndpoint string = workload.outputs.foundryEndpoint

@description('Foundry 프로젝트 이름')
output foundryProjectName string = workload.outputs.foundryProjectName

@description('Foundry Private Endpoint 이름')
output foundryPrivateEndpointName string = workload.outputs.foundryPrivateEndpointName

@description('Firewall Policy 이름')
output firewallPolicyName string = firewallPolicy.outputs.name

@description('Azure Firewall 이름')
output firewallName string = firewall.outputs.name

@description('Azure Firewall 사설 IP(실제 할당값)')
output firewallPrivateIpAddress string = firewall.outputs.privateIpAddress

@description('Azure Firewall 공인 IP')
output firewallPublicIpAddress string = firewall.outputs.publicIpAddress
