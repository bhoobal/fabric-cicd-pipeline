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
