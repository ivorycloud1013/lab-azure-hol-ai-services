metadata description = '''
Azure Firewall Policy 모듈. Private 망의 "화이트리스트만 통과" 정책을 정의한다.

규칙 그룹 우선순위
  200 rcg-network-allow      : L3/L4 서비스 태그 화이트리스트 (Entra ID, ARM, Azure Monitor 등)
  300 rcg-application-allow  : L7 FQDN 화이트리스트 (Foundry 엔드포인트, 포털, 패키지 저장소)
  400 rcg-deny-all           : 명시적 Deny-All. 방화벽 기본 동작도 deny지만,
                               로그에 "차단됨"이 남도록 명시 규칙을 둔다.

Azure Firewall은 규칙 그룹을 병렬로 갱신할 수 없으므로 dependsOn으로 순차 배포를 강제한다.
'''

@description('Firewall Policy 이름')
param name string

@description('배포 리전')
param location string

@description('리소스 태그')
param tags object = {}

@description('Firewall Policy SKU. 연결할 Azure Firewall의 SKU와 반드시 일치해야 한다.')
@allowed(['Basic', 'Standard', 'Premium'])
param skuTier string = 'Basic'

@description('규칙이 적용될 출발지 CIDR 목록(Private 망 워크로드 서브넷)')
param sourceAddresses string[]

@description('L4로 허용할 Azure 서비스 태그 목록')
param allowedServiceTags string[] = [
  'AzureActiveDirectory'
  'AzureResourceManager'
  'AzureMonitor'
  'AzureFrontDoor.FirstParty'
]

@description('Entra ID 인증 및 Azure 관리 평면 FQDN 화이트리스트')
param identityAndManagementFqdns string[] = [
  'login.microsoftonline.com'
  'login.microsoft.com'
  'login.windows.net'
  '*.login.microsoftonline.com'
  'management.azure.com'
  'graph.microsoft.com'
  '*.msauth.net'
  '*.msftauth.net'
  '*.aadcdn.msftauth.net'
]

@description('Azure AI Foundry 데이터/제어 평면 FQDN 화이트리스트')
param foundryFqdns string[] = [
  'ai.azure.com'
  '*.ai.azure.com'
  '*.services.ai.azure.com'
  '*.openai.azure.com'
  '*.cognitiveservices.azure.com'
  '*.azureml.ms'
  '*.api.azureml.ms'
]

@description('Azure Portal FQDN 화이트리스트. 점프박스 브라우저에서 포털을 쓰려면 필요하다.')
param portalFqdns string[] = [
  'portal.azure.com'
  '*.portal.azure.com'
  '*.portal.azure.net'
  '*.hosting.portal.azure.net'
  '*.ext.azure.com'
]

@description('실습 도구(Python 패키지 등) FQDN 화이트리스트')
param toolingFqdns string[] = [
  'pypi.org'
  '*.pypi.org'
  'files.pythonhosted.org'
  'aka.ms'
  '*.azureedge.net'
]

@description('실습 중 추가로 열어야 하는 FQDN. 기본 목록을 건드리지 않고 확장할 때 사용한다.')
param additionalAllowedFqdns string[] = []

@description('Windows Update 등 Azure Firewall FQDN 태그로 허용할 목록')
param allowedFqdnTags string[] = [
  'WindowsUpdate'
  'WindowsDiagnostics'
  'MicrosoftActiveProtectionService'
]

@description('위협 인텔리전스 모드. Basic SKU는 Alert만 지원한다.')
@allowed(['Off', 'Alert', 'Deny'])
param threatIntelMode string = 'Alert'

var httpAndHttpsProtocols = [
  {
    protocolType: 'Http'
    port: 80
  }
  {
    protocolType: 'Https'
    port: 443
  }
]

resource policy 'Microsoft.Network/firewallPolicies@2025-07-01' = {
  name: name
  location: location
  tags: tags
  properties: {
    sku: {
      tier: skuTier
    }
    threatIntelMode: threatIntelMode
  }
}

resource networkAllowRules 'Microsoft.Network/firewallPolicies/ruleCollectionGroups@2025-07-01' = {
  parent: policy
  name: 'rcg-network-allow'
  properties: {
    priority: 200
    ruleCollections: [
      {
        ruleCollectionType: 'FirewallPolicyFilterRuleCollection'
        name: 'allow-azure-service-tags'
        priority: 210
        action: {
          type: 'Allow'
        }
        rules: [
          {
            ruleType: 'NetworkRule'
            name: 'allow-control-plane-https'
            sourceAddresses: sourceAddresses
            destinationAddresses: allowedServiceTags
            destinationPorts: ['443']
            ipProtocols: ['TCP']
          }
        ]
      }
    ]
  }
}

resource applicationAllowRules 'Microsoft.Network/firewallPolicies/ruleCollectionGroups@2025-07-01' = {
  parent: policy
  name: 'rcg-application-allow'
  properties: {
    priority: 300
    ruleCollections: [
      {
        ruleCollectionType: 'FirewallPolicyFilterRuleCollection'
        name: 'allow-fqdn-whitelist'
        priority: 310
        action: {
          type: 'Allow'
        }
        rules: [
          {
            ruleType: 'ApplicationRule'
            name: 'allow-identity-and-management'
            sourceAddresses: sourceAddresses
            protocols: httpAndHttpsProtocols
            targetFqdns: identityAndManagementFqdns
          }
          {
            ruleType: 'ApplicationRule'
            name: 'allow-ai-foundry'
            sourceAddresses: sourceAddresses
            protocols: httpAndHttpsProtocols
            targetFqdns: foundryFqdns
          }
          {
            ruleType: 'ApplicationRule'
            name: 'allow-azure-portal'
            sourceAddresses: sourceAddresses
            protocols: httpAndHttpsProtocols
            targetFqdns: portalFqdns
          }
          {
            ruleType: 'ApplicationRule'
            name: 'allow-lab-tooling'
            sourceAddresses: sourceAddresses
            protocols: httpAndHttpsProtocols
            targetFqdns: toolingFqdns
          }
          {
            ruleType: 'ApplicationRule'
            name: 'allow-fqdn-tags'
            sourceAddresses: sourceAddresses
            protocols: httpAndHttpsProtocols
            fqdnTags: allowedFqdnTags
          }
        ]
      }
    ]
  }
  dependsOn: [
    networkAllowRules
  ]
}

resource additionalApplicationAllowRules 'Microsoft.Network/firewallPolicies/ruleCollectionGroups@2025-07-01' = if (!empty(additionalAllowedFqdns)) {
  parent: policy
  name: 'rcg-application-allow-additional'
  properties: {
    priority: 350
    ruleCollections: [
      {
        ruleCollectionType: 'FirewallPolicyFilterRuleCollection'
        name: 'allow-additional-fqdn-whitelist'
        priority: 360
        action: {
          type: 'Allow'
        }
        rules: [
          {
            ruleType: 'ApplicationRule'
            name: 'allow-additional-fqdns'
            sourceAddresses: sourceAddresses
            protocols: httpAndHttpsProtocols
            targetFqdns: additionalAllowedFqdns
          }
        ]
      }
    ]
  }
  dependsOn: [
    applicationAllowRules
  ]
}

resource denyAllRules 'Microsoft.Network/firewallPolicies/ruleCollectionGroups@2025-07-01' = {
  parent: policy
  name: 'rcg-deny-all'
  properties: {
    priority: 400
    ruleCollections: [
      {
        ruleCollectionType: 'FirewallPolicyFilterRuleCollection'
        name: 'deny-all'
        priority: 410
        action: {
          type: 'Deny'
        }
        rules: [
          {
            ruleType: 'NetworkRule'
            name: 'deny-all-outbound'
            sourceAddresses: ['*']
            destinationAddresses: ['*']
            destinationPorts: ['*']
            ipProtocols: ['Any']
          }
        ]
      }
    ]
  }
  dependsOn: [
    applicationAllowRules
    additionalApplicationAllowRules
  ]
}

@description('Firewall Policy 리소스 ID')
output id string = policy.id

@description('Firewall Policy 이름')
output name string = policy.name
