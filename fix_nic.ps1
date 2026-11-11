$ErrorActionPreference = 'Continue'
Write-Host 'Disabling EEE / ULPMode / FlowControl ...'
Set-NetAdapterAdvancedProperty -Name '以太网' -RegistryKeyword '*EEE' -RegistryValue 0
Set-NetAdapterAdvancedProperty -Name '以太网' -RegistryKeyword 'ULPMode' -RegistryValue 0
Set-NetAdapterAdvancedProperty -Name '以太网' -RegistryKeyword '*FlowControl' -RegistryValue 0
Write-Host 'Restarting adapter ...'
Restart-NetAdapter -Name '以太网'
Start-Sleep -Seconds 3
Get-NetAdapterAdvancedProperty -Name '以太网' |
    Where-Object { $_.RegistryKeyword -in '*EEE', 'ULPMode', '*FlowControl' } |
    Format-Table DisplayName, DisplayValue
Write-Host 'Done. You can close this window.'
