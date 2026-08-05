metadata description = '''
Private 망 점프박스 VM 모듈.

공인 IP를 붙이지 않는다. 접속은 Azure Bastion을 통해서만 가능하고,
아웃바운드는 서브넷의 UDR을 통해 Azure Firewall로 강제 터널링된다.

VM에 SystemAssigned 관리 ID를 부여해, Foundry 접근 시 키 없이
Entra ID 토큰(DefaultAzureCredential -> IMDS)을 사용할 수 있게 한다.
'''

@description('VM 이름. Windows 컴퓨터 이름 제한 때문에 15자 이하여야 한다.')
@maxLength(15)
param name string

@description('배포 리전')
param location string

@description('리소스 태그')
param tags object = {}

@description('VM을 배치할 서브넷 리소스 ID')
param subnetId string

@description('VM 크기')
param vmSize string = 'Standard_D2s_v5'

@description('관리자 계정 이름')
param adminUsername string

@secure()
@description('관리자 비밀번호')
param adminPassword string

@description('OS 디스크 유형')
@allowed(['Premium_LRS', 'StandardSSD_LRS', 'Standard_LRS'])
param osDiskType string = 'StandardSSD_LRS'

@description('이미지 publisher')
param imagePublisher string = 'MicrosoftWindowsServer'

@description('이미지 offer')
param imageOffer string = 'WindowsServer'

@description('이미지 SKU')
param imageSku string = '2022-datacenter-azure-edition'

resource networkInterface 'Microsoft.Network/networkInterfaces@2025-07-01' = {
  name: 'nic-${name}'
  location: location
  tags: tags
  properties: {
    ipConfigurations: [
      {
        name: 'ipconfig-primary'
        properties: {
          privateIPAllocationMethod: 'Dynamic'
          subnet: {
            id: subnetId
          }
          // 공인 IP 없음. Bastion 경유 접속만 허용한다.
        }
      }
    ]
  }
}

resource virtualMachine 'Microsoft.Compute/virtualMachines@2026-03-01' = {
  name: name
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    hardwareProfile: {
      vmSize: vmSize
    }
    securityProfile: {
      securityType: 'TrustedLaunch'
      uefiSettings: {
        secureBootEnabled: true
        vTpmEnabled: true
      }
    }
    storageProfile: {
      imageReference: {
        publisher: imagePublisher
        offer: imageOffer
        sku: imageSku
        version: 'latest'
      }
      osDisk: {
        name: 'osdisk-${name}'
        createOption: 'FromImage'
        deleteOption: 'Delete'
        managedDisk: {
          storageAccountType: osDiskType
        }
      }
    }
    osProfile: {
      computerName: name
      adminUsername: adminUsername
      adminPassword: adminPassword
      windowsConfiguration: {
        enableAutomaticUpdates: true
        provisionVMAgent: true
        patchSettings: {
          patchMode: 'AutomaticByPlatform'
          assessmentMode: 'ImageDefault'
        }
      }
    }
    networkProfile: {
      networkInterfaces: [
        {
          id: networkInterface.id
          properties: {
            deleteOption: 'Delete'
          }
        }
      ]
    }
  }
}

@description('VM 리소스 ID')
output id string = virtualMachine.id

@description('VM 이름')
output name string = virtualMachine.name

@description('VM 시스템 할당 관리 ID의 principal ID. Foundry RBAC 부여 대상이다.')
output principalId string = virtualMachine.identity.principalId

@description('VM 사설 IP')
output privateIpAddress string = networkInterface.properties.ipConfigurations[0].properties.privateIPAddress
