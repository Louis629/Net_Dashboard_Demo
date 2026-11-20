param(
    [Parameter(Mandatory=$true)]
    [string]$TargetComputer
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
    $errObj = @{ Error = if ($lastError) { "WinRM Error: $lastError" } else { "WinRM Offline or Host Unreachable" } }
    Write-Output ($errObj | ConvertTo-Json -Compress)
    exit 1
}

$RemoteScript = {
    $adapters = Get-NetAdapter -Physical | Where-Object { $_.Status -eq 'Up' -or $_.Status -eq 'Disconnected' }
    
    $results = @()
    foreach ($adapter in $adapters) {
        $type = if ($adapter.InterfaceDescription -match "Wi-Fi|Wireless|802.11") { "Wireless" } else { "Wired" }
        
        # FIX: Safely extract the first full IP address string
        $ipInfo = Get-NetIPAddress -InterfaceAlias $adapter.Name -AddressFamily IPv4 -ErrorAction SilentlyContinue | Select-Object -ExpandProperty IPAddress -First 1
        $ipAddress = if ($ipInfo) { $ipInfo } else { "No IP Assigned" }
        
        $results += @{
            Name = $adapter.Name
            Type = $type
            MacAddress = $adapter.MacAddress.Replace("-", ":").ToUpper()
            IPAddress = $ipAddress
            Status = $adapter.Status
        }
    }
    
    return ($results | ConvertTo-Json -Compress)
}

$Result = Invoke-Command -Session $Session -ScriptBlock $RemoteScript
Remove-PSSession $Session

Write-Output $Result