# Save as: scripts/Provision-ZtpDhcpScope.ps1
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$NetworkAddress,
    [Parameter(Mandatory)][int]$PrefixLength,
    [Parameter(Mandatory)][string]$ScopeName,
    
    # Infrastructure Defaults (overridable via environment or parameters)
    [string]$PrimaryServer = $(if ($env:ZTP_DHCP_PRIMARY_SERVER) { $env:ZTP_DHCP_PRIMARY_SERVER } else { 'dhcp01.corp.internal' }),
    [string]$PartnerServer = $(if ($env:ZTP_DHCP_PARTNER_SERVER) { $env:ZTP_DHCP_PARTNER_SERVER } else { 'dhcp02.corp.internal' }),
    [string]$FailoverRelationshipName = $(if ($env:ZTP_DHCP_FAILOVER_RELATIONSHIP) { $env:ZTP_DHCP_FAILOVER_RELATIONSHIP } else { '' }),
    [string[]]$DnsServers = $(if ($env:ZTP_DHCP_DNS_SERVERS) { $env:ZTP_DHCP_DNS_SERVERS -split ',' } else { @() }),
    [string]$DnsDomain = $(if ($env:ZTP_DHCP_DNS_DOMAIN) { $env:ZTP_DHCP_DNS_DOMAIN } elseif ($env:LDAP_DOMAIN) { $env:LDAP_DOMAIN } else { 'corp.internal' }),
    [int]$LeaseDays = $(if ($env:ZTP_DHCP_LEASE_DAYS) { [int]$env:ZTP_DHCP_LEASE_DAYS } else { 1 })
)

$ErrorActionPreference = 'Stop'

# Normalize comma-separated DNS server values passed via -File CLI invocation
$resolvedDnsServers = @()
foreach ($entry in $DnsServers) {
    if ($entry) {
        foreach ($part in ($entry -split ',')) {
            $trimmed = $part.Trim()
            if ($trimmed) { $resolvedDnsServers += $trimmed }
        }
    }
}

# --- IP Math Helper Functions (Runs Locally) ---
function Convert-IPToUInt32 ([string]$IpAddress) {
    $bytes = ([System.Net.IPAddress]::Parse($IpAddress)).GetAddressBytes()
    [Array]::Reverse($bytes)
    return [System.BitConverter]::ToUInt32($bytes, 0)
}
function Convert-UInt32ToIP ([UInt32]$Value) {
    $bytes = [System.BitConverter]::GetBytes($Value)
    [Array]::Reverse($bytes)
    return ([System.Net.IPAddress]::new($bytes)).IPAddressToString
}

# --- Calculate Boundaries (Runs Locally) ---
$netInt = Convert-IPToUInt32 -IpAddress $NetworkAddress
$childSize = [uint32][System.Math]::Pow(2, (32 - $PrefixLength))

$maskInt = [uint32]0
for ($i = 0; $i -lt $PrefixLength; $i++) { $maskInt = $maskInt -bor ([uint32]1 -shl (31 - $i)) }
$SubnetMask = Convert-UInt32ToIP -Value $maskInt

$StartRange = Convert-UInt32ToIP -Value ($netInt + 1)
$EndRange   = Convert-UInt32ToIP -Value ($netInt + $childSize - 2)
$Gateway    = Convert-UInt32ToIP -Value ($netInt + 1)
$ExclStart  = Convert-UInt32ToIP -Value ($netInt + 1)
$ExclEnd    = Convert-UInt32ToIP -Value ($netInt + 5) # Exclude first 5 usable IPs

try {
    # --- Remote Execution Block ---
    # Bypasses missing local RSAT module by executing directly on the DHCP server
    $RemoteScript = {
        param($NetAddr, $PriSrv, $ScpName, $StRng, $EdRng, $SubMsk, $LseDays, $ExStrt, $ExEnd, $Gtway, $DnsSrvs, $DnsDom, $FailName)

        Import-Module DhcpServer -ErrorAction Stop

        # 1. Check if scope already exists
        $existing = Get-DhcpServerv4Scope -ComputerName $PriSrv -ScopeId $NetAddr -ErrorAction SilentlyContinue
        if ($existing) {
            Write-Output "Scope $NetAddr already exists on $PriSrv. Updating scope name to '$ScpName'..."
            Set-DhcpServerv4Scope -ComputerName $PriSrv -ScopeId $NetAddr -Name $ScpName -Confirm:$false
            Write-Output "Successfully updated DHCP scope name for $NetAddr."
            return
        }

        # 2. Create Scope
        Add-DhcpServerv4Scope -ComputerName $PriSrv -Name $ScpName -StartRange $StRng -EndRange $EdRng -SubnetMask $SubMsk -State Active -LeaseDuration (New-TimeSpan -Days $LseDays) -Confirm:$false
        
        # 3. Add Exclusions
        Add-DhcpServerv4ExclusionRange -ComputerName $PriSrv -ScopeId $NetAddr -StartRange $ExStrt -EndRange $ExEnd -Confirm:$false
        
        # 4. Set Options
        $optionParams = @{
            ComputerName = $PriSrv
            ScopeId      = $NetAddr
            Router       = $Gtway
            DnsDomain    = $DnsDom
            Confirm      = $false
        }
        if ($DnsSrvs -and $DnsSrvs.Count -gt 0) {
            $optionParams['DnsServer'] = $DnsSrvs
        }
        Set-DhcpServerv4OptionValue @optionParams
        
        # 5. Bind to Failover Relationship (Dynamically Discovered)
        $activeFailover = Get-DhcpServerv4Failover -ComputerName $PriSrv -ErrorAction SilentlyContinue | Select-Object -First 1
        
        if ($activeFailover) {
            Write-Output "Binding scope to discovered failover relationship: $($activeFailover.Name)..."
            Add-DhcpServerv4FailoverScope -ComputerName $PriSrv -Name $activeFailover.Name -ScopeId $NetAddr -Confirm:$false
        } else {
            Write-Output "Warning: No active Failover Relationship found on $PriSrv. Scope provisioned as standalone."
        }

        Write-Output "Successfully provisioned new DHCP scope $NetAddr ($ScpName)."
    }

    # Dispatch the command to the target server
    Invoke-Command -ComputerName $PrimaryServer -ScriptBlock $RemoteScript -ArgumentList $NetworkAddress, $PrimaryServer, $ScopeName, $StartRange, $EndRange, $SubnetMask, $LeaseDays, $ExclStart, $ExclEnd, $Gateway, $resolvedDnsServers, $DnsDomain, $FailoverRelationshipName
}
catch {
    Write-Error "Failed to provision DHCP Scope: $($_.Exception.Message)"
    exit 1
}