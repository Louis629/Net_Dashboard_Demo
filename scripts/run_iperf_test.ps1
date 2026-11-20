param(
    [Parameter(Mandatory=$true)]
    [string]$TargetComputer,
    
    [Parameter(Mandatory=$true)]
    [string]$ServerList  # Format: "Hub1|10.x.x.x,Hub2|10.y.y.y"
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
    $detailMsg = if ($lastError) { " Detailed error: $lastError" } else { "WinRM Error" }
    $errObj = @(@{ DataCenter="All"; Status="Down"; AvgLatency="-"; PacketLoss="-"; DownloadThroughput="WinRM Error: $detailMsg"; UploadThroughput="N/A" })
    Write-Output ($errObj | ConvertTo-Json -Compress)
    exit 1
}

$RemoteScript = {
    param($ServersStr)
    
    $MasterResults = @()
    $iPerfPath = "C:\Tools\iPerf\iperf3.exe"
    $iPerfExists = Test-Path $iPerfPath
    
    # Split the incoming string into individual servers
    $ServerArray = $ServersStr -split ','
    
    foreach ($srv in $ServerArray) {
        $parts = $srv -split '\|'
        $dcName = $parts[0]
        $ServerIP = $parts[1]
        
        $resultObj = @{
            DataCenter = $dcName
            Status = "Down"
            AvgLatency = "-"
            PacketLoss = "100%"
            DownloadThroughput = "-"
            UploadThroughput = "-"
        }
        
        # 1. PING TEST
        $pingOutput = ping.exe $ServerIP -n 10 | Out-String
        
        $lossMatch = [regex]::Match($pingOutput, '\((?<loss>\d+)%\s*loss\)')
        if ($lossMatch.Success) { $resultObj.PacketLoss = $lossMatch.Groups['loss'].Value + "%" }
        
        $avgMatch = [regex]::Match($pingOutput, '(?i)Average\s*=\s*(?<avg>\d+ms)')
        if ($avgMatch.Success) { $resultObj.AvgLatency = $avgMatch.Groups['avg'].Value }
        
        if ($resultObj.PacketLoss -ne "100%") {
            $resultObj.Status = "Up"
        }
        
        # 2. BIDIRECTIONAL IPERF TEST PIPELINE
        if ($resultObj.Status -eq "Up") {
            if ($iPerfExists) {
                # --- PASS A: DOWNLOAD TEST (Reverse Mode - Pulls data from server) ---
                $dlOut = & $iPerfPath -c $ServerIP -t 3 -f m -P 5 -R 2>&1 | Out-String
                $sumRegex = '(?i)\[SUM\].*?\s+(?<speed>\d+(?:\.\d+)?)\s+(?<unit>[KMG]bits/sec).*?receiver'
                $dlMatch = [regex]::Match($dlOut, $sumRegex)
                
                if ($dlMatch.Success) {
                    $speed = [math]::Round([double]$dlMatch.Groups['speed'].Value, 1)
                    $resultObj.DownloadThroughput = "$speed Mbps"
                } else {
                    $resultObj.DownloadThroughput = "0.0 Mbps (Parse Error)"
                }

                # --- PASS B: UPLOAD TEST (Standard Mode - Pushes data to server) ---
                $ulOut = & $iPerfPath -c $ServerIP -t 3 -f m -P 5 2>&1 | Out-String
                $ulMatch = [regex]::Match($ulOut, $sumRegex)
                
                if ($ulMatch.Success) {
                    $speed = [math]::Round([double]$ulMatch.Groups['speed'].Value, 1)
                    $resultObj.UploadThroughput = "$speed Mbps"
                } else {
                    $resultObj.UploadThroughput = "0.0 Mbps (Parse Error)"
                }
            } else {
                $resultObj.DownloadThroughput = "Missing iPerf3"
                $resultObj.UploadThroughput = "Missing iPerf3"
            }
        }
        $MasterResults += $resultObj
    }
    
    return ($MasterResults | ConvertTo-Json -Compress)
}

$Result = Invoke-Command -Session $Session -ScriptBlock $RemoteScript -ArgumentList $ServerList
Remove-PSSession $Session

Write-Output $Result