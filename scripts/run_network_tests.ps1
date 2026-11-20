# Save as: scripts/run_network_tests.ps1
param(
    [Parameter(Mandatory=$true)]
    [string]$TargetComputer,
    
    [Parameter(Mandatory=$true)]
    [string]$Destination
)

# Auto-discover administrative service account credentials and AD domain settings from environment or .env
$cred = $null
$bindUser = $env:LDAP_BIND_USER
$bindPass = $env:LDAP_BIND_PASSWORD
$ldapDomain = $env:LDAP_DOMAIN
$netbiosDomain = $env:AD_NETBIOS_DOMAIN

$envPaths = @(".\.env", "..\.env", "..\..\.env")
foreach ($ep in $envPaths) {
    if ($bindUser -and $bindPass -and $ldapDomain) { break }
    if (Test-Path $ep) {
        $lines = Get-Content $ep -ErrorAction SilentlyContinue
        foreach ($line in $lines) {
            if (-not $bindUser -and $line.StartsWith("LDAP_BIND_USER=")) {
                $bindUser = $line.Substring("LDAP_BIND_USER=".Length).Trim("'`" ")
            }
            if (-not $bindPass -and $line.StartsWith("LDAP_BIND_PASSWORD=")) {
                $bindPass = $line.Substring("LDAP_BIND_PASSWORD=".Length).Trim("'`" ")
            }
            if (-not $ldapDomain -and $line.StartsWith("LDAP_DOMAIN=")) {
                $ldapDomain = $line.Substring("LDAP_DOMAIN=".Length).Trim("'`" ")
            }
            if (-not $netbiosDomain -and $line.StartsWith("AD_NETBIOS_DOMAIN=")) {
                $netbiosDomain = $line.Substring("AD_NETBIOS_DOMAIN=".Length).Trim("'`" ")
            }
        }
    }
}

if (-not $ldapDomain -and $env:USERDNSDOMAIN) {
    $ldapDomain = $env:USERDNSDOMAIN.ToLower()
}
if (-not $netbiosDomain -and $env:USERDOMAIN) {
    $netbiosDomain = $env:USERDOMAIN
}

if ($bindUser -and $bindPass) {
    try {
        $domainUser = if ($bindUser.Contains("\") -or $bindUser.Contains("@")) {
            $bindUser
        } elseif ($netbiosDomain) {
            "$netbiosDomain\$bindUser"
        } elseif ($ldapDomain) {
            "$bindUser@$ldapDomain"
        } else {
            $bindUser
        }
        $secPass = New-Object System.Security.SecureString
        $bindPass.ToCharArray() | ForEach-Object { $secPass.AppendChar($_) }
        $cred = New-Object System.Management.Automation.PSCredential($domainUser, $secPass)
    } catch {}
}

# Build candidate hostname targets (short name and domain FQDN)
$computerCandidates = @($TargetComputer)
if (-not $TargetComputer.Contains(".")) {
    if ($ldapDomain) {
        $configuredFqdn = "$TargetComputer.$($ldapDomain.ToLower())"
        if ($computerCandidates -notcontains $configuredFqdn) {
            $computerCandidates += $configuredFqdn
        }
    }
    if ($env:USERDNSDOMAIN) {
        $fqdnDomain = "$TargetComputer.$($env:USERDNSDOMAIN.ToLower())"
        if ($computerCandidates -notcontains $fqdnDomain) {
            $computerCandidates += $fqdnDomain
        }
    }
}

$Session = $null
$lastError = $null

foreach ($target in $computerCandidates) {
    if ($cred) {
        try {
            $Session = New-PSSession -ComputerName $target -Credential $cred -ErrorAction Stop
            if ($Session) { break }
        } catch {
            $lastError = $_.Exception.Message
        }
    }

    try {
        $Session = New-PSSession -ComputerName $target -ErrorAction Stop
        if ($Session) { break }
    } catch {
        $lastError = $_.Exception.Message
    }
}

if (-not $Session) {
    $detailMsg = if ($lastError) { " Detailed error: $lastError" } else { "" }
    $errObj = @{ Error = "Failed to establish WinRM session to $TargetComputer.$detailMsg" }
    Write-Output ($errObj | ConvertTo-Json -Compress)
    exit 1
}

$RemoteScript = {
    param($Dest)
    
    # --- 1. RUN AND PARSE PING ---
    $pingRaw = ping.exe $Dest -n 4 | Out-String
    
    $Loss = "-"
    $Avg  = "-"
    if ($pingRaw -match '\((?<loss>\d+)%\s*loss\)') { $Loss = $Matches['loss'] + "%" }
    if ($pingRaw -match 'Average\s*=\s*(?<avg>\d+ms)') { $Avg = $Matches['avg'] }
    
    $PingSummary = @{
        Destination = $Dest
        PacketLoss  = $Loss
        AvgLatency  = $Avg
        Status      = if ($Loss -eq "100%") { "Unreachable" } else { "Reachable" }
    }
    
    # --- 2. RUN AND PARSE TRACEROUTE ---
    # Running without reverse DNS (-d) and limiting to 20 hops for raw speed
    $traceRaw = tracert.exe -d -h 20 $Dest | Out-String
    $Hops = @()
    
    # Split raw output into lines and process lines containing hop data
    $Lines = $traceRaw -split "`r?`n"
    foreach ($Line in $Lines) {
        # Matches typical tracert format: "  1     1 ms     1 ms     1 ms  10.1.1.1"
        if ($Line -match '^\s*(?<hop>\d+)\s+(?<rtt1>[<*\d]+.*?ms|[*])\s+(?<rtt2>[<*\d]+.*?ms|[*])\s+(?<rtt3>[<*\d]+.*?ms|[*])\s+(?<ip>[a-zA-Z0-9.-]+|\*)') {
            $Hops += @{
                Hop  = $Matches['hop'].Trim()
                Rtt1 = $Matches['rtt1'].Trim()
                Rtt2 = $Matches['rtt2'].Trim()
                Rtt3 = $Matches['rtt3'].Trim()
                IP   = $Matches['ip'].Trim()
            }
        }
    }
    
    # Combine everything into a structured response bundle
    $Response = @{
        Ping = $PingSummary
        Hops = $Hops
    }
    
    return ($Response | ConvertTo-Json -Compress)
}

$Result = Invoke-Command -Session $Session -ScriptBlock $RemoteScript -ArgumentList $Destination
Remove-PSSession $Session

Write-Output $Result