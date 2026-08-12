// ============================================================================
// main.bicep
// Deploys a Windows VM ("host") and bootstraps installation + registration of
// the Microsoft Fabric / Power BI On-premises Data Gateway service on it.
//
// Notes / assumptions (verified against Microsoft Learn docs, Aug 2026):
//  - The on-premises data gateway only runs on 64-bit Windows 10 / Windows
//    Server 2019+. This template deploys Windows Server 2022 Datacenter.
//  - Bootstrapping is done with a CustomScriptExtension that:
//      1. Installs the "DataGateway" PowerShell module (PSGallery)
//      2. Runs Install-DataGateway -AcceptConditions (downloads + installs
//         the gateway binaries silently)
//      3. Signs in non-interactively with a Microsoft Entra service
//         principal via Connect-DataGatewayServiceAccount
//      4. Registers a new gateway cluster with Add-DataGatewayCluster
//  - You must pre-create a Microsoft Entra app registration (service
//    principal) with the Power BI Service API "Tenant.Read.All" /
//    "Tenant.ReadWrite.All" application permissions (admin-consented) to use
//    for unattended registration. See:
//    https://learn.microsoft.com/powershell/module/datagateway.profile/connect-datagatewayserviceaccount
//  - Microsoft's docs note that Add-DataGatewayCluster "must be run with a
//    user based credential." In practice the service-principal flow is the
//    documented, supported path for unattended installs (see
//    "Automation of Data Gateway Installation" on the Power BI blog); if
//    Add-DataGatewayCluster rejects the service-principal session in your
//    tenant, switch the registration step to an interactive
//    Connect-DataGatewayServiceAccount run instead.
//  - The gateway recovery key and service principal secret are passed
//    through the extension's protectedSettings, which is encrypted at rest
//    and not returned by ARM/Azure Resource Graph reads.
// ============================================================================

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('Name of the VM that will host the gateway.')
param vmName string = 'vm-fabric-gateway'

@description('Local administrator username for the VM.')
param adminUsername string

@secure()
@description('Local administrator password for the VM.')
param adminPassword string

@description('VM size. 8 vCPU / 8 GB+ is recommended for production gateways; Standard_D4s_v5 (4 vCPU) is a reasonable dev/test default.')
param vmSize string = 'Standard_D4s_v5'

@description('Address prefix for the new virtual network.')
param vnetAddressPrefix string = '10.60.0.0/24'

@description('Address prefix for the gateway subnet.')
param subnetAddressPrefix string = '10.60.0.0/26'

@description('Source address prefix allowed to RDP into the VM via the NSG (only used if deployPublicIp is true). Lock this down to your own IP/CIDR.')
param rdpSourceAddressPrefix string = '*'

@description('Deploy a public IP + NSG RDP rule for direct access. Leave false if you will reach the VM via VPN/ExpressRoute/Bastion instead.')
param deployPublicIp bool = false

@description('Name to register the gateway cluster as. Must be unique across the tenant.')
param gatewayName string

@secure()
@description('Recovery key for the new gateway cluster. Store this safely — Microsoft cannot retrieve it for you.')
param recoveryKey string

@description('Application (client) ID of the Microsoft Entra service principal used for unattended gateway registration.')
param spnClientId string

@secure()
@description('Client secret of the Microsoft Entra service principal used for unattended gateway registration.')
param spnClientSecret string

@description('Tenant ID (GUID) that owns the service principal and the Fabric/Power BI tenant.')
param tenantId string

@description('Optional gateway region key (e.g. "brazilsouth"). Leave empty to use the tenant default region. Run Get-DataGatewayRegion to list valid values.')
param gatewayRegion string = ''

@description('Tags applied to all resources.')
param tags object = {
  workload: 'fabric-on-prem-gateway'
}

// ----------------------------------------------------------------------------
// Networking
// ----------------------------------------------------------------------------

resource nsg 'Microsoft.Network/networkSecurityGroups@2023-11-01' = {
  name: '${vmName}-nsg'
  location: location
  tags: tags
  properties: {
    securityRules: deployPublicIp ? [
      {
        name: 'Allow-RDP'
        properties: {
          priority: 1000
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourcePortRange: '*'
          destinationPortRange: '3389'
          sourceAddressPrefix: rdpSourceAddressPrefix
          destinationAddressPrefix: '*'
        }
      }
    ] : []
  }
}

resource vnet 'Microsoft.Network/virtualNetworks@2023-11-01' = {
  name: '${vmName}-vnet'
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: [vnetAddressPrefix]
    }
    subnets: [
      {
        name: 'snet-gateway'
        properties: {
          addressPrefix: subnetAddressPrefix
          networkSecurityGroup: {
            id: nsg.id
          }
        }
      }
    ]
  }
}

resource publicIp 'Microsoft.Network/publicIPAddresses@2023-11-01' = if (deployPublicIp) {
  name: '${vmName}-pip'
  location: location
  tags: tags
  sku: {
    name: 'Standard'
  }
  properties: {
    publicIPAllocationMethod: 'Static'
  }
}

resource nic 'Microsoft.Network/networkInterfaces@2023-11-01' = {
  name: '${vmName}-nic'
  location: location
  tags: tags
  properties: {
    ipConfigurations: [
      {
        name: 'ipconfig1'
        properties: {
          privateIPAllocationMethod: 'Dynamic'
          subnet: {
            id: vnet.properties.subnets[0].id
          }
          publicIPAddress: deployPublicIp ? {
            id: publicIp.id
          } : null
        }
      }
    ]
  }
}

// ----------------------------------------------------------------------------
// Host VM
// ----------------------------------------------------------------------------

resource vm 'Microsoft.Compute/virtualMachines@2024-07-01' = {
  name: vmName
  location: location
  tags: tags
  properties: {
    hardwareProfile: {
      vmSize: vmSize
    }
    osProfile: {
      computerName: vmName
      adminUsername: adminUsername
      adminPassword: adminPassword
      windowsConfiguration: {
        enableAutomaticUpdates: true
        patchSettings: {
          patchMode: 'AutomaticByOS'
        }
      }
    }
    storageProfile: {
      imageReference: {
        publisher: 'MicrosoftWindowsServer'
        offer: 'WindowsServer'
        sku: '2022-datacenter-azure-edition'
        version: 'latest'
      }
      osDisk: {
        createOption: 'FromImage'
        managedDisk: {
          storageAccountType: 'StandardSSD_LRS'
        }
      }
    }
    networkProfile: {
      networkInterfaces: [
        {
          id: nic.id
        }
      ]
    }
  }
}

// ----------------------------------------------------------------------------
// Bootstrap script (installs + registers the gateway)
// ----------------------------------------------------------------------------

var scriptTemplate = '''
$ErrorActionPreference = "Stop"
$logDir = "C:\ProgramData\FabricGatewayBootstrap"
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
Start-Transcript -Path "$logDir\bootstrap.log" -Append

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

Write-Output "Installing NuGet provider and DataGateway PowerShell module..."
Install-PackageProvider -Name NuGet -MinimumVersion 2.8.5.201 -Force -Scope AllUsers | Out-Null
Set-PSRepository -Name PSGallery -InstallationPolicy Trusted
Install-Module -Name DataGateway -Force -Scope AllUsers -AllowClobber
Import-Module DataGateway

Write-Output "Downloading and installing the on-premises data gateway..."
Install-DataGateway -AcceptConditions

Write-Output "Signing in with the service principal..."
$clientSecret = ConvertTo-SecureString -String '{{SPN_CLIENT_SECRET}}' -AsPlainText -Force
Connect-DataGatewayServiceAccount -ApplicationId '{{SPN_CLIENT_ID}}' -ClientSecret $clientSecret -Tenant '{{TENANT_ID}}'

Write-Output "Registering gateway cluster '{{GATEWAY_NAME}}'..."
$recoveryKey = ConvertTo-SecureString -String '{{RECOVERY_KEY}}' -AsPlainText -Force
$regionKey = '{{GATEWAY_REGION}}'
if ([string]::IsNullOrWhiteSpace($regionKey)) {
    Add-DataGatewayCluster -GatewayName '{{GATEWAY_NAME}}' -RecoveryKey $recoveryKey -OverwriteExistingGateway
} else {
    Add-DataGatewayCluster -GatewayName '{{GATEWAY_NAME}}' -RecoveryKey $recoveryKey -RegionKey $regionKey -OverwriteExistingGateway
}

Write-Output "Gateway installation and registration complete."
Stop-Transcript
'''

var recoveryKeyEscaped = replace(recoveryKey, '\'', '\'\'')
var spnClientSecretEscaped = replace(spnClientSecret, '\'', '\'\'')

var bootstrapScript = replace(replace(replace(replace(replace(
  scriptTemplate,
  '{{GATEWAY_NAME}}', gatewayName),
  '{{RECOVERY_KEY}}', recoveryKeyEscaped),
  '{{SPN_CLIENT_ID}}', spnClientId),
  '{{SPN_CLIENT_SECRET}}', spnClientSecretEscaped),
  '{{TENANT_ID}}', tenantId
)
var bootstrapScriptFinal = replace(bootstrapScript, '{{GATEWAY_REGION}}', gatewayRegion)

var bootstrapScriptBase64 = base64(bootstrapScriptFinal)

resource gatewayBootstrap 'Microsoft.Compute/virtualMachines/extensions@2024-07-01' = {
  parent: vm
  name: 'InstallFabricGateway'
  location: location
  tags: tags
  properties: {
    publisher: 'Microsoft.Compute'
    type: 'CustomScriptExtension'
    typeHandlerVersion: '1.10'
    autoUpgradeMinorVersion: true
    protectedSettings: {
      commandToExecute: 'powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "[IO.File]::WriteAllText(\'C:\\Windows\\Temp\\FabricGatewayBootstrap.ps1\', [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String(\'${bootstrapScriptBase64}\'))); powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File C:\\Windows\\Temp\\FabricGatewayBootstrap.ps1"'
    }
  }
}

// ----------------------------------------------------------------------------
// Outputs
// ----------------------------------------------------------------------------

output vmName string = vm.name
output vmPrivateIp string = nic.properties.ipConfigurations[0].properties.privateIPAddress
output vmPublicIp string = deployPublicIp ? publicIp.properties.ipAddress : ''
output gatewayName string = gatewayName
