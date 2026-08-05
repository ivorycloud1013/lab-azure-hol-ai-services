metadata description = '''
"VNet에 붙는 모든 subnet은 NSG를 가져야 한다"를 Azure Policy로 강제하는 모듈.
(구독 범위에 정의를 만들고, 대상 리소스 그룹에 할당한다.)

Bicep의 subnetConfig 타입이 배포 시점의 실수를 막는다면, 이 정책은 배포 이후
포털/CLI로 NSG 없는 서브넷이 추가되는 것을 막는 런타임 가드레일이다.

effect 기본값은 Audit이다. Deny로 올리면 정책 할당이 VNet 생성보다 먼저 평가될 때
배포 자체가 실패할 수 있으므로, 인프라를 한 번 배포한 뒤에 올리는 것을 권장한다.

NSG를 붙일 수 없는 플랫폼 서브넷(AzureFirewallSubnet 등)은 exemptSubnetNames로 제외한다.
'''

targetScope = 'subscription'

@description('정책 정의/할당 이름 접미사')
param nameSuffix string

@description('정책을 할당할 리소스 그룹 이름')
param targetResourceGroupName string

@description('정책 효과')
@allowed(['Audit', 'Deny', 'Disabled'])
param effect string = 'Audit'

@description('NSG 연결이 면제되는 서브넷 이름. NSG를 지원하지 않는 플랫폼 서브넷만 넣는다.')
param exemptSubnetNames string[] = [
  'AzureFirewallSubnet'
  'AzureFirewallManagementSubnet'
  'GatewaySubnet'
  'RouteServerSubnet'
]

var policyDefinitionName = 'policy-subnet-requires-nsg-${nameSuffix}'
var policyAssignmentName = 'assign-subnet-requires-nsg-${nameSuffix}'

resource policyDefinition 'Microsoft.Authorization/policyDefinitions@2023-04-01' = {
  name: policyDefinitionName
  properties: {
    policyType: 'Custom'
    mode: 'Indexed'
    displayName: '모든 서브넷은 NSG를 연결해야 함'
    description: 'NSG가 연결되지 않은 서브넷 생성을 감사하거나 거부한다. 플랫폼 예외 서브넷은 제외한다.'
    metadata: {
      category: 'Network'
    }
    parameters: {
      effect: {
        type: 'String'
        allowedValues: ['Audit', 'Deny', 'Disabled']
        defaultValue: 'Audit'
        metadata: {
          displayName: '효과'
        }
      }
      exemptSubnetNames: {
        type: 'Array'
        defaultValue: []
        metadata: {
          displayName: '면제 서브넷 이름'
        }
      }
    }
    policyRule: {
      if: {
        anyOf: [
          // 1) 서브넷을 자식 리소스로 따로 만드는 경우
          {
            allOf: [
              {
                field: 'type'
                equals: 'Microsoft.Network/virtualNetworks/subnets'
              }
              {
                field: 'name'
                notIn: '[parameters(\'exemptSubnetNames\')]'
              }
              {
                field: 'Microsoft.Network/virtualNetworks/subnets/networkSecurityGroup.id'
                exists: 'false'
              }
            ]
          }
          // 2) VNet 안에 서브넷을 인라인으로 정의하는 경우
          {
            allOf: [
              {
                field: 'type'
                equals: 'Microsoft.Network/virtualNetworks'
              }
              {
                count: {
                  field: 'Microsoft.Network/virtualNetworks/subnets[*]'
                  where: {
                    allOf: [
                      {
                        field: 'Microsoft.Network/virtualNetworks/subnets[*].name'
                        notIn: '[parameters(\'exemptSubnetNames\')]'
                      }
                      {
                        field: 'Microsoft.Network/virtualNetworks/subnets[*].networkSecurityGroup.id'
                        exists: 'false'
                      }
                    ]
                  }
                }
                greater: 0
              }
            ]
          }
        ]
      }
      then: {
        effect: '[parameters(\'effect\')]'
      }
    }
  }
}

module policyAssignment './policy-assignment.bicep' = {
  scope: resourceGroup(targetResourceGroupName)
  name: 'assign-${policyAssignmentName}'
  params: {
    name: policyAssignmentName
    displayName: '모든 서브넷은 NSG를 연결해야 함'
    policyDefinitionId: policyDefinition.id
    enforcementMode: effect == 'Disabled' ? 'DoNotEnforce' : 'Default'
    policyParameters: {
      effect: {
        value: effect
      }
      exemptSubnetNames: {
        value: exemptSubnetNames
      }
    }
  }
}

@description('정책 정의 리소스 ID')
output policyDefinitionId string = policyDefinition.id

@description('정책 할당 리소스 ID')
output policyAssignmentId string = policyAssignment.outputs.id
