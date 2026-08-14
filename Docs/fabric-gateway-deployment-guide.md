# Deploying the Microsoft Fabric On-Premises Data Gateway with Bicep (AVM)

**Owner:** _add name_
**Status:** Draft
**Last updated:** 2026-08-14
**Labels:** `azure` `bicep` `fabric` `power-bi` `data-gateway` `avm`

> **ℹ️ Summary**
> This page walks through deploying a host VM and installing/registering the Microsoft Fabric (Power BI) On-premises Data Gateway on it, entirely via Infrastructure-as-Code using Bicep and Azure Verified Modules (AVM). The VM has no public IP — all administrative access goes through Azure Bastion.

---

## 1. Overview

This deployment provisions:

| Component | Purpose | AVM module | Version |
|---|---|---|---|
| Network Security Group | Restricts inbound RDP to Bastion subnet only | `avm/res/network/network-security-group` | `0.5.3` |
| Virtual Network | Hosts the gateway subnet + `AzureBastionSubnet` | `avm/res/network/virtual-network` | `0.9.0` |
| Azure Bastion | Only path for RDP access to the VM | `avm/res/network/bastion-host` | `0.8.2` |
| Virtual Machine | Windows Server 2022 host for the gateway | `avm/res/compute/virtual-machine` | `0.22.3` |

The VM's `extensionCustomScriptConfig` runs a PowerShell bootstrap script on first boot that:

1. Installs the `DataGateway` PowerShell module from PSGallery
2. Runs `Install-DataGateway -AcceptConditions` to silently install the gateway binaries
3. Signs in non-interactively via `Connect-DataGatewayServiceAccount` using a Microsoft Entra service principal
4. Registers the gateway cluster via `Add-DataGatewayCluster`

> **⚠️ Warning**
> Microsoft's docs note that `Add-DataGatewayCluster` "must be run with a user based credential." In practice, the service-principal flow is the documented, supported path for unattended installs (see *Automation of Data Gateway Installation* on the Power BI blog). If `Add-DataGatewayCluster` is rejected under a service-principal session in your tenant, switch the registration step to an interactive `Connect-DataGatewayServiceAccount` run instead.

---

## 2. Prerequisites

### 2.1 Tooling

- Azure CLI ≥ 2.60, with the Bicep tooling installed (`az bicep install`)
- Contributor (or equivalent) access on the target subscription/resource group
- Network connectivity from your workstation to Azure (for `az deployment group create` and, later, Bastion)

### 2.2 Microsoft Entra service principal (for unattended gateway registration)

The bootstrap script signs in non-interactively, so you need an app registration with Power BI Service API permissions **before** you deploy.

1. Create the app registration:

   ```bash
   az ad app create --display-name "fabric-gateway-automation"
   ```

2. Create a client secret for it and record the value (shown only once):

   ```bash
   az ad app credential reset --id <appId> --display-name "gateway-bootstrap" --years 1
   ```

3. Grant the Power BI Service API application permissions `Tenant.Read.All` and `Tenant.ReadWrite.All`, then have a tenant/global admin grant admin consent. The quickest path is the Azure Portal: **App registrations → your app → API permissions → Add a permission → APIs my organization uses → Power BI Service → Application permissions** → select both → **Grant admin consent**.

4. Note down three values for later: **Application (client) ID**, **client secret**, **Directory (tenant) ID**.

> **ℹ️ Note**
> This is the same service principal pattern documented at [Connect-DataGatewayServiceAccount](https://learn.microsoft.com/en-us/powershell/module/datagateway.profile/connect-datagatewayserviceaccount).

### 2.3 Gateway recovery key

Decide on a recovery key value now. This encrypts/decrypts on-prem credentials for the gateway and is required to restore it or add cluster members later. **Microsoft cannot retrieve this for you if it's lost** — store it in your secrets manager (Key Vault, etc.), not in source control.

### 2.4 Network planning

Default address plan used by the sample below — adjust to fit your environment:

| Subnet | CIDR | Notes |
|---|---|---|
| VNet | `10.60.0.0/24` | |
| `snet-gateway` | `10.60.0.0/26` | Hosts the VM |
| `AzureBastionSubnet` | `10.60.0.64/26` | Name is fixed by Azure; must be `/26` or larger |

---

## 3. Step-by-step deployment

### Step 1 — Lock down inbound access with an NSG

Create a Network Security Group that only allows RDP (3389) from the Bastion subnet — never from the internet.

```bicep
module nsg 'br/public:avm/res/network/network-security-group:0.5.3' = {
  name: 'nsg-deployment'
  params: {
    name: '${vmName}-nsg'
    location: location
    tags: tags
    securityRules: [
      {
        name: 'Allow-RDP-From-Bastion'
        properties: {
          priority: 1000
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourcePortRange: '*'
          destinationPortRange: '3389'
          sourceAddressPrefix: bastionSubnetAddressPrefix
          destinationAddressPrefix: '*'
        }
      }
    ]
  }
}
```

### Step 2 — Create the virtual network and subnets

```bicep
module vnet 'br/public:avm/res/network/virtual-network:0.9.0' = {
  name: 'vnet-deployment'
  params: {
    name: '${vmName}-vnet'
    location: location
    tags: tags
    addressPrefixes: [vnetAddressPrefix]
    subnets: [
      {
        name: 'snet-gateway'
        addressPrefix: subnetAddressPrefix
        networkSecurityGroupResourceId: nsg.outputs.resourceId
      }
      {
        // Name is fixed by Azure Bastion requirements.
        name: 'AzureBastionSubnet'
        addressPrefix: bastionSubnetAddressPrefix
      }
    ]
  }
}
```

> **ℹ️ Note**
> `avm/res/network/virtual-network` looks up the `AzureBastionSubnet` name literally — it must match exactly, and Bastion requires `/26` or larger.

### Step 3 — Deploy Azure Bastion

Bastion is the *only* way to reach the VM over RDP — it provisions its own public IP internally, so the VM stays fully private.

```bicep
module bastionHost 'br/public:avm/res/network/bastion-host:0.8.2' = {
  name: 'bastion-deployment'
  params: {
    name: '${vmName}-bastion'
    location: location
    tags: tags
    skuName: bastionSkuName        // 'Basic' is sufficient for standard RDP
    virtualNetworkResourceId: vnet.outputs.resourceId
  }
}
```

### Step 4 — Build the bootstrap script

The bootstrap script is templated in Bicep with `{{TOKEN}}` placeholders, then substituted with `replace()` and passed to the VM's extension as base64 so it can be safely embedded in a single-line `commandToExecute`.

```bicep
var scriptTemplate = '''
$ErrorActionPreference = "Stop"
$logDir = "C:\ProgramData\FabricGatewayBootstrap"
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
Start-Transcript -Path "$logDir\bootstrap.log" -Append

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

Install-PackageProvider -Name NuGet -MinimumVersion 2.8.5.201 -Force -Scope AllUsers | Out-Null
Set-PSRepository -Name PSGallery -InstallationPolicy Trusted
Install-Module -Name DataGateway -Force -Scope AllUsers -AllowClobber
Import-Module DataGateway

Install-DataGateway -AcceptConditions

$clientSecret = ConvertTo-SecureString -String '{{SPN_CLIENT_SECRET}}' -AsPlainText -Force
Connect-DataGatewayServiceAccount -ApplicationId '{{SPN_CLIENT_ID}}' -ClientSecret $clientSecret -Tenant '{{TENANT_ID}}'

$recoveryKey = ConvertTo-SecureString -String '{{RECOVERY_KEY}}' -AsPlainText -Force
$regionKey = '{{GATEWAY_REGION}}'
if ([string]::IsNullOrWhiteSpace($regionKey)) {
    Add-DataGatewayCluster -GatewayName '{{GATEWAY_NAME}}' -RecoveryKey $recoveryKey -OverwriteExistingGateway
} else {
    Add-DataGatewayCluster -GatewayName '{{GATEWAY_NAME}}' -RecoveryKey $recoveryKey -RegionKey $regionKey -OverwriteExistingGateway
}

Stop-Transcript
'''

var bootstrapScript = replace(replace(replace(replace(replace(
  scriptTemplate,
  '{{GATEWAY_NAME}}', gatewayName),
  '{{RECOVERY_KEY}}', replace(recoveryKey, '\'', '\'\'')),
  '{{SPN_CLIENT_ID}}', spnClientId),
  '{{SPN_CLIENT_SECRET}}', replace(spnClientSecret, '\'', '\'\'')),
  '{{TENANT_ID}}', tenantId
)
var bootstrapScriptFinal = replace(bootstrapScript, '{{GATEWAY_REGION}}', gatewayRegion)
var bootstrapScriptBase64 = base64(bootstrapScriptFinal)
```

> **⚠️ Warning**
> Keep the recovery key and client secret flowing only through `@secure()` params and `protectedSettings` (Step 5). Never log the decoded script or print it to deployment output.

### Step 5 — Deploy the VM with the bootstrap extension

`avm/res/compute/virtual-machine` provisions its own NIC from `nicConfigurations` (there's no way to attach a pre-built NIC), and runs the bootstrap script through its built-in `extensionCustomScriptConfig`.

```bicep
module vm 'br/public:avm/res/compute/virtual-machine:0.22.3' = {
  name: 'vm-deployment'
  params: {
    name: vmName
    computerName: vmName
    location: location
    tags: tags
    vmSize: vmSize                 // e.g. Standard_D4s_v5
    osType: 'Windows'
    adminUsername: adminUsername
    adminPassword: adminPassword
    availabilityZone: -1
    patchMode: 'AutomaticByOS'
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
    nicConfigurations: [
      {
        nicSuffix: '-nic'
        ipConfigurations: [
          {
            name: 'ipconfig01'
            subnetResourceId: vnet.outputs.subnetResourceIds[0]
          }
        ]
      }
    ]
    extensionCustomScriptConfig: {
      protectedSettings: {
        commandToExecute: 'powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "[IO.File]::WriteAllText(\'C:\\Windows\\Temp\\FabricGatewayBootstrap.ps1\', [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String(\'${bootstrapScriptBase64}\'))); powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File C:\\Windows\\Temp\\FabricGatewayBootstrap.ps1"'
      }
    }
  }
}
```

### Step 6 — Validate and deploy

```bash
# Compile / lint locally
bicep build main.bicep

# What-if preview
az deployment group what-if \
  --resource-group rg-fabric-gateway \
  --template-file main.bicep \
  --parameters main.bicepparam

# Deploy
az deployment group create \
  --resource-group rg-fabric-gateway \
  --template-file main.bicep \
  --parameters main.bicepparam
```

Sample `main.bicepparam`:

```bicep
using 'main.bicep'

param vmName = 'vm-fabric-gateway-01'
param adminUsername = 'gwadmin'
param adminPassword = readEnvironmentVariable('VM_ADMIN_PASSWORD')

param gatewayName = 'contoso-fabric-gateway'
param recoveryKey = readEnvironmentVariable('GATEWAY_RECOVERY_KEY')

param spnClientId = '00000000-0000-0000-0000-000000000000'
param spnClientSecret = readEnvironmentVariable('GATEWAY_SPN_SECRET')
param tenantId = '11111111-1111-1111-1111-111111111111'

param bastionSkuName = 'Basic'
```

Export the secrets as environment variables before running the deployment, e.g.:

```bash
export VM_ADMIN_PASSWORD="..."
export GATEWAY_RECOVERY_KEY="..."
export GATEWAY_SPN_SECRET="..."
```

### Step 7 — Verify the gateway registered

1. Sign in to [app.powerbi.com](https://app.powerbi.com) (or the Fabric portal) → **Settings → Manage connections and gateways**.
2. Confirm a gateway named per your `gatewayName` parameter shows status **Online**.
3. If it's missing or offline, connect to the VM (Step 8) and check `C:\ProgramData\FabricGatewayBootstrap\bootstrap.log` for the transcript of the install/registration steps.

### Step 8 — Connect for troubleshooting (via Bastion only)

There is no public IP on the VM — connect through Bastion:

```bash
az network bastion rdp \
  --name <bastionHostName-output> \
  --resource-group rg-fabric-gateway \
  --target-resource-id <vm-resource-id>
```

Or use the **Connect → Bastion** blade on the VM resource in the Azure Portal.

---

## 4. Post-deployment checklist

- [ ] Gateway shows **Online** in the Power BI / Fabric admin portal
- [ ] Recovery key stored securely (Key Vault or equivalent), not left in shell history
- [ ] Service principal secret rotated/expiring per your org's policy
- [ ] NSG confirmed to allow RDP only from `AzureBastionSubnet`, not `0.0.0.0/0`
- [ ] `bootstrap.log` on the VM reviewed for errors
- [ ] Gateway added to a cluster / second member deployed for HA (out of scope of this template — see [Add another gateway to create a cluster](https://learn.microsoft.com/en-us/data-integration/gateway/service-gateway-install#add-another-gateway-to-create-a-cluster))

---

## 5. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Extension fails, `bootstrap.log` never created | Script failed to decode/write to disk | Check `commandToExecute` wasn't truncated (Azure limits the extension command length); re-run `bicep build` to confirm the base64 payload is intact |
| `Add-DataGatewayCluster` throws an auth/permission error | Service principal not admin-consented, or tenant enforces user-based registration | Re-check API permissions consent (§2.2); as a fallback, RDP in via Bastion and run the registration step interactively |
| Gateway installs but never appears online | Outbound network/firewall blocking Azure Relay endpoints | Confirm outbound HTTPS (443) is allowed from the VM's subnet; gateway requires outbound internet access |
| `Add-DataGatewayCluster` fails with a name conflict | `gatewayName` already exists in the tenant | Pick a unique name, or add `-OverwriteExistingGateway` (already included in the template) |
| Can't RDP to the VM | No public IP by design | Use Bastion (§Step 8) — direct RDP is intentionally blocked |

---

## 6. References

- [Install an on-premises data gateway – Microsoft Learn](https://learn.microsoft.com/en-us/data-integration/gateway/service-gateway-install)
- [PowerShell Cmdlets for On-premises data gateway management – Microsoft Learn](https://learn.microsoft.com/en-us/powershell/gateway/overview?view=datagateway-ps)
- [DataGateway Module reference – Microsoft Learn](https://learn.microsoft.com/en-us/powershell/module/datagateway/?view=datagateway-ps)
- [Connect-DataGatewayServiceAccount – Microsoft Learn](https://learn.microsoft.com/en-us/powershell/module/datagateway.profile/connect-datagatewayserviceaccount?view=datagateway-ps)
- [Add-DataGatewayCluster – Microsoft Learn](https://learn.microsoft.com/en-us/powershell/module/datagateway/add-datagatewaycluster?view=datagateway-ps)
- [Install-DataGateway – Microsoft Learn](https://learn.microsoft.com/en-us/powershell/module/datagateway/install-datagateway?view=datagateway-ps)
- [Azure Verified Modules – Bicep Registry Modules (GitHub)](https://github.com/Azure/bicep-registry-modules)
  - [`avm/res/compute/virtual-machine`](https://github.com/Azure/bicep-registry-modules/tree/main/avm/res/compute/virtual-machine)
  - [`avm/res/network/virtual-network`](https://github.com/Azure/bicep-registry-modules/tree/main/avm/res/network/virtual-network)
  - [`avm/res/network/bastion-host`](https://github.com/Azure/bicep-registry-modules/tree/main/avm/res/network/bastion-host)
  - [`avm/res/network/network-security-group`](https://github.com/Azure/bicep-registry-modules/tree/main/avm/res/network/network-security-group)

---

## 7. Appendix — Full source files

The complete `main.bicep` and `main.bicepparam` used throughout this guide are attached to this page / available in the linked repository. See the accompanying `main.bicep` and `main.bicepparam` files shared alongside this guide.
