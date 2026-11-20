# Save as: scripts/get_teap_logs.ps1
<#
.SYNOPSIS
    Gathers comprehensive TEAP / 802.1X diagnostic logs, active profiles,
    machine certificates, and user certificate stores for the currently logged-in user.
.DESCRIPTION
    Connects to the remote computer using WinRM (running under the administrative service account).
    Collects network profiles, 802.1X event logs, Sonic XML profiles, GPO registry extensions,
    Collects network profiles, 802.1X event logs, configured XML profiles, GPO registry extensions,
    and extracts full certificate metadata from:
      1. LocalMachine\My (Computer Certificate Store)
      2. LocalMachine\CA and LocalMachine\Root (Enterprise CAs)
      3. CurrentUser\My for the currently logged-in interactive user (and loaded user profiles)
         via profile disk stores (%APPDATA%\Microsoft\SystemCertificates\My) and HKEY_USERS registry.
    Generates structured CSV files, a human-readable certificate readiness report, and exports
    public .cer certificates into a compressed ZIP archive for download.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)]
    [string]$TargetComputer,

    [Parameter(Mandatory=$false)]
    [switch]$UseStdinCredentials = $false
)

# Use a relative temporary path so Python knows exactly where to find the ZIP
$LocalDestPath = ".\temp_downloads\$TargetComputer"

# Auto-discover AD domain settings and TEAP diagnostic filters from environment or .env
$bindUser = $env:LDAP_BIND_USER
$bindPass = $env:LDAP_BIND_PASSWORD
$ldapDomain = $env:LDAP_DOMAIN
$netbiosDomain = $env:AD_NETBIOS_DOMAIN
$xmlProfilesCsv = $env:TEAP_XML_PROFILES
$caFilter = $env:TEAP_CA_FILTER

$envPaths = @(".\.env", "..\.env", "..\..\.env")
foreach ($ep in $envPaths) {
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
            if (-not $xmlProfilesCsv -and $line.StartsWith("TEAP_XML_PROFILES=")) {
                $xmlProfilesCsv = $line.Substring("TEAP_XML_PROFILES=".Length).Trim("'`" ")
            }
            if (-not $caFilter -and $line.StartsWith("TEAP_CA_FILTER=")) {
                $caFilter = $line.Substring("TEAP_CA_FILTER=".Length).Trim("'`" ")
            }
        }
        break
    }
}

if (-not $ldapDomain -and $env:USERDNSDOMAIN) {
    $ldapDomain = $env:USERDNSDOMAIN.ToLower()
}
if (-not $netbiosDomain -and $env:USERDOMAIN) {
    $netbiosDomain = $env:USERDOMAIN
}

# Parse optional step-up credentials passed securely via stdin JSON
$cred = $null
try {
    if ($UseStdinCredentials) {
        $rawStdin = [Console]::In.ReadToEnd()
        if ($rawStdin -and $rawStdin.Trim() -ne "") {
            $inputData = $rawStdin | ConvertFrom-Json
            if ($inputData.username -and $inputData.password) {
                $rawUser = $inputData.username.ToString().Trim()
                $domainUser = if (-not ($rawUser.Contains("\") -or $rawUser.Contains("@"))) {
                    "US\$rawUser"
                $domainUser = if ($rawUser.Contains("\") -or $rawUser.Contains("@")) {
                    $rawUser
                } elseif ($netbiosDomain) {
                    "$netbiosDomain\$rawUser"
                } elseif ($ldapDomain) {
                    "$rawUser@$ldapDomain"
                } else {
                    $rawUser
                }
                $secPass = New-Object System.Security.SecureString
                $inputData.password.ToString().ToCharArray() | ForEach-Object { $secPass.AppendChar($_) }
                $cred = New-Object System.Management.Automation.PSCredential($domainUser, $secPass)
            }
        }
    }
} catch {
    Write-Warning "Failed to parse credentials from stdin: $($_.Exception.Message)"
}

# Auto-discover administrative service account credentials from environment or .env if not supplied via stdin
if (-not $cred) {
    $bindUser = $env:LDAP_BIND_USER
    $bindPass = $env:LDAP_BIND_PASSWORD

    if (-not ($bindUser -and $bindPass)) {
        $envPaths = @(".\.env", "..\.env", "..\..\.env")
        foreach ($ep in $envPaths) {
            if (Test-Path $ep) {
                $lines = Get-Content $ep -ErrorAction SilentlyContinue
                foreach ($line in $lines) {
                    if ($line.StartsWith("LDAP_BIND_USER=")) {
                        $bindUser = $line.Substring("LDAP_BIND_USER=".Length).Trim("'`" ")
                    }
                    if ($line.StartsWith("LDAP_BIND_PASSWORD=")) {
                        $bindPass = $line.Substring("LDAP_BIND_PASSWORD=".Length).Trim("'`" ")
                    }
                }
                if ($bindUser -and $bindPass) { break }
            }
# Fall back to administrative service account credentials from environment or .env if not supplied via stdin
if (-not $cred -and $bindUser -and $bindPass) {
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
    } catch {
        Write-Warning "Failed to construct service account credentials: $($_.Exception.Message)"
    }

    if ($bindUser -and $bindPass) {
        try {
            $domainUser = if (-not ($bindUser.Contains("\") -or $bindUser.Contains("@"))) {
                "US\$bindUser"
            } else {
                $bindUser
            }
            $secPass = New-Object System.Security.SecureString
            $bindPass.ToCharArray() | ForEach-Object { $secPass.AppendChar($_) }
            $cred = New-Object System.Management.Automation.PSCredential($domainUser, $secPass)
        } catch {
            Write-Warning "Failed to construct service account credentials: $($_.Exception.Message)"
        }
    }
}

# Build candidate hostname targets (short name and domain FQDN)
$computerCandidates = @($TargetComputer)
if (-not $TargetComputer.Contains(".")) {
    $computerCandidates += "$TargetComputer.corp.internal"
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

Write-Host "Connecting to $TargetComputer..."
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
    $detailMsg = if ($lastError) { " Detailed error: $lastError." } else { "" }
    Write-Error "Failed to connect to $TargetComputer via WinRM.$detailMsg Ensure the device is online and WinRM is active."
    exit 1
}

$RemoteScript = {
    param(
        [string]$XmlProfilesCsv = "",
        [string]$CaFilter = ""
    )
    $RemotePath = "C:\Temp\Network_Diagnostics"
    if (-not (Test-Path $RemotePath)) { New-Item -ItemType Directory -Path $RemotePath -Force | Out-Null }

    # =========================================================================
    # 1. Sonic XML Profile Files Check
    # 1. Configured XML Profile Files Check
    # =========================================================================
    $TargetFiles = @("C:\Temp\Sonic-SOE.xml", "C:\Temp\Sonic-Wired-SOE.xml", "C:\Temp\Sonic-Wired-SOE-Production.xml")
    $TargetFiles = @()
    if ($XmlProfilesCsv -and $XmlProfilesCsv.Trim() -ne "") {
        $TargetFiles = @($XmlProfilesCsv -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne '' })
    }
    $XmlLogFile = "$RemotePath\xml_file_check.txt"
    "--- Sonic XML File Presence Check ---`n" | Out-File $XmlLogFile
    "--- XML Profile File Presence Check ---`n" | Out-File $XmlLogFile

    foreach ($File in $TargetFiles) {
        if (Test-Path $File) {
            $FileInfo = Get-Item $File
            "FOUND: $File" | Out-File $XmlLogFile -Append
            "  - Size: $($FileInfo.Length) bytes" | Out-File $XmlLogFile -Append
            Copy-Item -Path $File -Destination $RemotePath -Force
        } else {
            "MISSING: $File" | Out-File $XmlLogFile -Append
    if ($TargetFiles.Count -eq 0) {
        "INFO: No custom TEAP_XML_PROFILES configured." | Out-File $XmlLogFile -Append
    } else {
        foreach ($File in $TargetFiles) {
            if (Test-Path $File) {
                $FileInfo = Get-Item $File
                "FOUND: $File" | Out-File $XmlLogFile -Append
                "  - Size: $($FileInfo.Length) bytes" | Out-File $XmlLogFile -Append
                Copy-Item -Path $File -Destination $RemotePath -Force
            } else {
                "MISSING: $File" | Out-File $XmlLogFile -Append
            }
        }
    }

    # =========================================================================
    # 2. Network Profiles and Interfaces
    # =========================================================================
    & C:\Windows\System32\netsh.exe wlan show profiles > "$RemotePath\active_wlan_profiles.txt"
    & C:\Windows\System32\netsh.exe lan show profiles > "$RemotePath\active_lan_profiles.txt"
    & C:\Windows\System32\netsh.exe wlan show interfaces > "$RemotePath\wlan_interfaces.txt"

    # =========================================================================
    # 3. Windows Event Logs (24-Hour Scope)
    # =========================================================================
    $StartTime = (Get-Date).AddHours(-24)
    $LogProviders = @{
        "WLAN-AutoConfig"  = "Microsoft-Windows-WLAN-AutoConfig/Operational"
        "Wired-AutoConfig" = "Microsoft-Windows-Wired-AutoConfig/Operational"
        "GroupPolicy"      = "Microsoft-Windows-GroupPolicy/Operational"
    }

    foreach ($Key in $LogProviders.Keys) {
        try {
            $Logs = Get-WinEvent -FilterHashtable @{LogName = $LogProviders[$Key]; StartTime = $StartTime} -ErrorAction SilentlyContinue
            if ($Logs) {
                $Logs | Select-Object TimeCreated, Id, LevelDisplayName, Message | Export-Csv -Path "$RemotePath\$Key`_Logs.csv" -NoTypeInformation
            }
        } catch {}
    }

    # =========================================================================
    # 4. Group Policy Network Policy Registry Keys
    # =========================================================================
    $GPO_WLAN_Path   = "HKLM:\Software\Policies\Microsoft\Windows\Wireless\GPTWirelessPolicy"
    $GPO_Wired_Path  = "HKLM:\Software\Policies\Microsoft\Windows\WiredL2\GPTExtension"
    if (Test-Path $GPO_WLAN_Path)  { Get-ChildItem $GPO_WLAN_Path  | Out-File "$RemotePath\gpo_wlan_reg.txt" }
    if (Test-Path $GPO_Wired_Path) { Get-ChildItem $GPO_Wired_Path | Out-File "$RemotePath\gpo_wired_reg.txt" }

    # =========================================================================
    # 5. Certificate Stores Collection (Machine & Current Logged-in User)
    # =========================================================================
    $CertDir = "$RemotePath\certificates"
    if (-not (Test-Path $CertDir)) { New-Item -ItemType Directory -Path $CertDir -Force | Out-Null }

    function Get-CertificateDetails {
        param(
            [System.Security.Cryptography.X509Certificates.X509Certificate2]$Cert,
            [string]$StoreScope = "LocalMachine",
            [string]$StoreName = "My",
            [string]$TargetUser = ""
        )

        $now = Get-Date
        $isExpired = ($Cert.NotAfter -lt $now)
        $isNotYetValid = ($Cert.NotBefore -gt $now)
        $daysRemaining = [math]::Round(($Cert.NotAfter - $now).TotalDays, 1)

        $ekuList = @()
        $sanList = @()
        $keyUsage = ""
        $templateName = ""

        foreach ($ext in $Cert.Extensions) {
            if ($ext.Oid.FriendlyName -eq "Enhanced Key Usage") {
                try {
                    $enhancedExt = [System.Security.Cryptography.X509Certificates.X509EnhancedKeyUsageExtension]$ext
                    foreach ($u in $enhancedExt.EnhancedKeyUsages) {
                        $ekuList += if ($u.FriendlyName) { "$($u.FriendlyName) ($($u.Value))" } else { $u.Value }
                    }
                } catch {
                    $ekuList += $ext.Format($false)
                }
            } elseif ($ext.Oid.FriendlyName -eq "Subject Alternative Name") {
                $sanList += $ext.Format($false)
            } elseif ($ext.Oid.FriendlyName -eq "Key Usage") {
                $keyUsage = $ext.Format($false)
            } elseif ($ext.Oid.Value -eq "1.3.6.1.4.1.311.21.7" -or $ext.Oid.FriendlyName -like "*Template*") {
                $templateName = $ext.Format($false)
            }
        }

        $hasClientAuth = ($ekuList -match "Client Authentication" -or $ekuList -match "1.3.6.1.5.5.7.3.2")
        $hasServerAuth = ($ekuList -match "Server Authentication" -or $ekuList -match "1.3.6.1.5.5.7.3.1")
        $validStatus = if ($isExpired) { "EXPIRED" } elseif ($isNotYetValid) { "NOT_YET_VALID" } else { "VALID" }
        $keyLength = try { $Cert.PublicKey.Key.KeySize } catch { "N/A" }

        return [PSCustomObject]@{
            StoreScope        = $StoreScope
            StoreName         = $StoreName
            UserAccount       = $TargetUser
            Subject           = $Cert.Subject
            Issuer            = $Cert.Issuer
            Thumbprint        = $Cert.Thumbprint
            SerialNumber      = $Cert.SerialNumber
            NotBefore         = $Cert.NotBefore.ToString("yyyy-MM-dd HH:mm:ss")
            NotAfter          = $Cert.NotAfter.ToString("yyyy-MM-dd HH:mm:ss")
            DaysRemaining     = $daysRemaining
            Status            = $validStatus
            HasPrivateKey     = [bool]$Cert.HasPrivateKey
            ClientAuthEKU     = [bool]$hasClientAuth
            ServerAuthEKU     = [bool]$hasServerAuth
            EnhancedKeyUsages = ($ekuList -join "; ")
            SubjectAltName    = ($sanList -join "; ")
            KeyUsage          = $keyUsage
            KeyAlgorithm      = $Cert.PublicKey.Oid.FriendlyName
            KeyLength         = $keyLength
            Template          = $templateName
        }
    }

    # --- 5a. Machine Certificates (LocalMachine\My) ---
    $machineCertDetails = @()
    $machineCerts = Get-ChildItem -Path Cert:\LocalMachine\My -ErrorAction SilentlyContinue
    if ($machineCerts) {
        foreach ($c in $machineCerts) {
            $details = Get-CertificateDetails -Cert $c -StoreScope "LocalMachine" -StoreName "My"
            $machineCertDetails += $details

            # Export public .cer certificate
            try {
                $cerPath = "$CertDir\machine_$($c.Thumbprint).cer"
                [System.IO.File]::WriteAllBytes($cerPath, $c.Export([System.Security.Cryptography.X509Certificates.X509ContentType]::Cert))
            } catch {}
        }
        $machineCertDetails | Export-Csv -Path "$RemotePath\machine_certificates.csv" -NoTypeInformation
    }

    # Collect Enterprise CA and Root certificates
    $caCerts = Get-ChildItem -Path Cert:\LocalMachine\CA, Cert:\LocalMachine\Root -ErrorAction SilentlyContinue | Where-Object { $_.Subject -like "*Sonic*" -or $_.Issuer -like "*Sonic*" }
    if ($CaFilter -and $CaFilter.Trim() -ne "") {
        $caPattern = "*$($CaFilter.Trim())*"
        $caCerts = Get-ChildItem -Path Cert:\LocalMachine\CA, Cert:\LocalMachine\Root -ErrorAction SilentlyContinue | Where-Object { $_.Subject -like $caPattern -or $_.Issuer -like $caPattern }
    } else {
        $caCerts = Get-ChildItem -Path Cert:\LocalMachine\CA -ErrorAction SilentlyContinue
    }
    if ($caCerts) {
        $caDetails = foreach ($ca in $caCerts) {
            Get-CertificateDetails -Cert $ca -StoreScope "LocalMachine" -StoreName "CA/Root"
        }
        $caDetails | Export-Csv -Path "$RemotePath\enterprise_ca_certificates.csv" -NoTypeInformation
    }

    # --- 5b. Logged-in User Certificates (CurrentUser\My) ---
    # The WinRM session executes as the Net Dashboard administrative service account.
    # The end user logged into the computer does not have local admin rights.
    # We inspect the active console user and loaded user profiles directly via disk & registry.
    $consoleUser = ""
    try {
        $cs = Get-CimInstance -ClassName Win32_ComputerSystem -ErrorAction SilentlyContinue
        if ($cs -and $cs.UserName) {
            $consoleUser = $cs.UserName.Trim()
        }
    } catch {}

    $userProfiles = @()
    try {
        $loaded = Get-CimInstance -ClassName Win32_UserProfile -ErrorAction SilentlyContinue | Where-Object { $_.Loaded -eq $true -and $_.Special -ne $true }
        if ($loaded) {
            $userProfiles = @($loaded)
        }
    } catch {}

    # If no active loaded profile, inspect interactive user profiles present on disk
    if ($userProfiles.Count -eq 0) {
        try {
            $allProfiles = Get-CimInstance -ClassName Win32_UserProfile -ErrorAction SilentlyContinue | Where-Object { $_.Special -ne $true -and $_.LocalPath -notlike "*Administrator*" -and $_.LocalPath -notlike "*Default*" }
            if ($allProfiles) {
                $userProfiles = @($allProfiles)
            }
        } catch {}
    }

    $userCertDetails = @()
    $inspectedUserSummaries = @()
    $processedUserThumbprints = @{}

    foreach ($prof in $userProfiles) {
        $userSid = $prof.SID
        $profPath = $prof.LocalPath

        $userDisplay = ""
        try {
            $sidObj = New-Object System.Security.Principal.SecurityIdentifier($userSid)
            $userDisplay = $sidObj.Translate([System.Security.Principal.NTAccount]).Value
        } catch {
            $userDisplay = if ($profPath) { Split-Path $profPath -Leaf } else { $userSid }
        }

        $cleanUserDisplay = ($userDisplay -replace '[\\/:]', '_').Trim()
        $isConsole = ($consoleUser -and ($userDisplay -like "*$($consoleUser.Replace('.\', ''))*" -or $consoleUser -like "*$userDisplay*"))

        $inspectedUserSummaries += [PSCustomObject]@{
            Username    = $userDisplay
            SID         = $userSid
            ProfilePath = $profPath
            IsConsole   = [bool]$isConsole
        }

        # A. Inspect File Store: %APPDATA%\Microsoft\SystemCertificates\My\Certificates
        $fileStorePath = "$profPath\AppData\Roaming\Microsoft\SystemCertificates\My\Certificates"
        if (Test-Path $fileStorePath) {
            $certFiles = Get-ChildItem -Path $fileStorePath -File -ErrorAction SilentlyContinue
            foreach ($cf in $certFiles) {
                try {
                    $bytes = [System.IO.File]::ReadAllBytes($cf.FullName)
                    $cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2(,$bytes)
                    $tp = $cert.Thumbprint
                    $key = "$userSid-$tp"
                    if (-not $processedUserThumbprints.ContainsKey($key)) {
                        $processedUserThumbprints[$key] = $true
                        $details = Get-CertificateDetails -Cert $cert -StoreScope "CurrentUser" -StoreName "My" -TargetUser $userDisplay
                        $userCertDetails += $details

                        # Export public .cer
                        try {
                            $userCerPath = "$CertDir\user_${cleanUserDisplay}_$tp.cer"
                            [System.IO.File]::WriteAllBytes($userCerPath, $cert.Export([System.Security.Cryptography.X509Certificates.X509ContentType]::Cert))
                        } catch {}
                    }
                } catch {}
            }
        }

        # B. Inspect Registry Store: HKEY_USERS\<SID>\Software\Microsoft\SystemCertificates\My\Certificates
        $regCertPath = "Registry::HKEY_USERS\$userSid\Software\Microsoft\SystemCertificates\My\Certificates"
        if (Test-Path $regCertPath) {
            $regKeys = Get-ChildItem -Path $regCertPath -ErrorAction SilentlyContinue
            foreach ($rk in $regKeys) {
                try {
                    $blob = (Get-ItemProperty -Path $rk.PSPath -ErrorAction SilentlyContinue).Blob
                    if ($blob) {
                        $cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2(,$blob)
                        $tp = $cert.Thumbprint
                        $key = "$userSid-$tp"
                        if (-not $processedUserThumbprints.ContainsKey($key)) {
                            $processedUserThumbprints[$key] = $true
                            $details = Get-CertificateDetails -Cert $cert -StoreScope "CurrentUser" -StoreName "My" -TargetUser $userDisplay
                            $userCertDetails += $details

                            try {
                                $userCerPath = "$CertDir\user_${cleanUserDisplay}_$tp.cer"
                                [System.IO.File]::WriteAllBytes($userCerPath, $cert.Export([System.Security.Cryptography.X509Certificates.X509ContentType]::Cert))
                            } catch {}
                        }
                    }
                } catch {}
            }
        }
    }

    if ($userCertDetails.Count -gt 0) {
        $userCertDetails | Export-Csv -Path "$RemotePath\user_certificates.csv" -NoTypeInformation
    }

    # --- 5c. Certificate Verification Summary Report ---
    $summaryFile = "$RemotePath\certificate_verification_summary.txt"
    $sb = New-Object System.Text.StringBuilder
    $null = $sb.AppendLine("================================================================================")
    $null = $sb.AppendLine("                    WINDOWS 802.1X / TEAP CERTIFICATE REPORT                    ")
    $null = $sb.AppendLine("================================================================================")
    $null = $sb.AppendLine("Target Computer : $env:COMPUTERNAME")
    $null = $sb.AppendLine("Generated At    : $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')")
    $null = $sb.AppendLine("Console User    : $(if ($consoleUser) { $consoleUser } else { 'None detected (Locked or background)' })")
    $null = $sb.AppendLine("Service Context : Admin (Elevated Net Dashboard Diagnostics)")
    $null = $sb.AppendLine("")
    $null = $sb.AppendLine("--------------------------------------------------------------------------------")
    $null = $sb.AppendLine("1. COMPUTER CERTIFICATE EVALUATION (Cert:\LocalMachine\My)")
    $null = $sb.AppendLine("--------------------------------------------------------------------------------")

    $validMachineCerts = @($machineCertDetails | Where-Object { $_.Status -eq "VALID" -and $_.ClientAuthEKU -eq $true -and $_.HasPrivateKey -eq $true })
    if ($validMachineCerts.Count -gt 0) {
        $null = $sb.AppendLine("Status: PASS (Valid computer certificate present with Private Key and Client Auth EKU)")
    } else {
        $null = $sb.AppendLine("Status: WARNING / FAIL (No valid computer certificate with Private Key and Client Auth EKU found!)")
    }
    $null = $sb.AppendLine("Total Certificates Found in LocalMachine\My: $($machineCertDetails.Count)")
    $null = $sb.AppendLine("")

    $idx = 1
    foreach ($mc in $machineCertDetails) {
        $null = $sb.AppendLine("  [$idx] Subject      : $($mc.Subject)")
        $null = $sb.AppendLine("      Issuer       : $($mc.Issuer)")
        $null = $sb.AppendLine("      Thumbprint   : $($mc.Thumbprint)")
        $null = $sb.AppendLine("      Validity     : $($mc.NotBefore) to $($mc.NotAfter) ($($mc.DaysRemaining) days remaining - $($mc.Status))")
        $null = $sb.AppendLine("      Private Key  : $(if ($mc.HasPrivateKey) { 'PRESENT' } else { 'MISSING' })")
        $null = $sb.AppendLine("      Client Auth  : $(if ($mc.ClientAuthEKU) { 'YES' } else { 'NO' })")
        $null = $sb.AppendLine("      Server Auth  : $(if ($mc.ServerAuthEKU) { 'YES' } else { 'NO' })")
        if ($mc.SubjectAltName) { $null = $sb.AppendLine("      SAN          : $($mc.SubjectAltName)") }
        if ($mc.Template) { $null = $sb.AppendLine("      Template     : $($mc.Template)") }
        $null = $sb.AppendLine("")
        $idx++
    }

    $null = $sb.AppendLine("--------------------------------------------------------------------------------")
    $null = $sb.AppendLine("2. LOGGED-IN USER CERTIFICATE EVALUATION (CurrentUser\My)")
    $null = $sb.AppendLine("--------------------------------------------------------------------------------")
    if ($inspectedUserSummaries.Count -gt 0) {
        $null = $sb.AppendLine("Inspected User Accounts:")
        foreach ($u in $inspectedUserSummaries) {
            $activeTag = if ($u.IsConsole) { " [ACTIVE CONSOLE USER]" } else { "" }
            $null = $sb.AppendLine("  - Username: $($u.Username)$activeTag (SID: $($u.SID))")
            $null = $sb.AppendLine("    Profile : $($u.ProfilePath)")
        }
    } else {
        $null = $sb.AppendLine("No interactive user profiles currently loaded.")
    }
    $null = $sb.AppendLine("")

    $validUserCerts = @($userCertDetails | Where-Object { $_.Status -eq "VALID" -and $_.ClientAuthEKU -eq $true -and $_.HasPrivateKey -eq $true })
    if ($validUserCerts.Count -gt 0) {
        $null = $sb.AppendLine("Status: PASS (Valid user certificate present with Private Key and Client Auth EKU)")
    } else {
        $null = $sb.AppendLine("Status: WARNING / FAIL (No valid user certificate with Private Key and Client Auth EKU found in user store!)")
    }
    $null = $sb.AppendLine("Total User Certificates Discovered: $($userCertDetails.Count)")
    $null = $sb.AppendLine("")

    $uidx = 1
    foreach ($uc in $userCertDetails) {
        $null = $sb.AppendLine("  [$uidx] User Account : $($uc.UserAccount)")
        $null = $sb.AppendLine("       Subject      : $($uc.Subject)")
        $null = $sb.AppendLine("       Issuer       : $($uc.Issuer)")
        $null = $sb.AppendLine("       Thumbprint   : $($uc.Thumbprint)")
        $null = $sb.AppendLine("       Validity     : $($uc.NotBefore) to $($uc.NotAfter) ($($uc.DaysRemaining) days remaining - $($uc.Status))")
        $null = $sb.AppendLine("       Private Key  : $(if ($uc.HasPrivateKey) { 'PRESENT' } else { 'MISSING' })")
        $null = $sb.AppendLine("       Client Auth  : $(if ($uc.ClientAuthEKU) { 'YES' } else { 'NO' })")
        if ($uc.SubjectAltName) { $null = $sb.AppendLine("       SAN (UPN)    : $($uc.SubjectAltName)") }
        if ($uc.Template) { $null = $sb.AppendLine("       Template     : $($uc.Template)") }
        $null = $sb.AppendLine("")
        $uidx++
    }

    $null = $sb.AppendLine("--------------------------------------------------------------------------------")
    $null = $sb.AppendLine("3. 802.1X / TEAP AUTHENTICATION READINESS VERDICT")
    $null = $sb.AppendLine("--------------------------------------------------------------------------------")
    $compReady = ($validMachineCerts.Count -gt 0)
    $userReady = ($validUserCerts.Count -gt 0)

    $null = $sb.AppendLine("Computer Auth Ready : $(if ($compReady) { 'YES (PASS)' } else { 'NO (FAIL - Missing or invalid machine certificate)' })")
    $null = $sb.AppendLine("User Auth Ready     : $(if ($userReady) { 'YES (PASS)' } else { 'NO (FAIL - Missing or invalid user certificate)' })")

    $overallVerdict = if ($compReady -and $userReady) {
        "READY FOR TEAP (Both computer and user certificates are valid and capable of 802.1X authentication)"
    } elseif ($compReady -and -not $userReady) {
        "PARTIALLY READY (Computer certificate valid; User authentication will fail until user cert is enrolled/renewed)"
    } elseif (-not $compReady -and $userReady) {
        "PARTIALLY READY (User certificate valid; Computer certificate missing or invalid; Outer tunnel may fail)"
    } else {
        "NOT READY (Both computer and user certificates require administrative remediation)"
    }
    $null = $sb.AppendLine("Overall Assessment  : $overallVerdict")
    $null = $sb.AppendLine("================================================================================")

    $sb.ToString() | Out-File -FilePath $summaryFile -Encoding utf8

    # =========================================================================
    # 6. Compress Output Package
    # =========================================================================
    $ZipPath = "C:\Temp\Network_Diagnostics.zip"
    if (Test-Path $ZipPath) { Remove-Item $ZipPath -Force }
    Compress-Archive -Path "$RemotePath\*" -DestinationPath $ZipPath -Force
    Remove-Item $RemotePath -Recurse -Force
}

Invoke-Command -Session $Session -ScriptBlock $RemoteScript
Invoke-Command -Session $Session -ScriptBlock $RemoteScript -ArgumentList ([string]$xmlProfilesCsv), ([string]$caFilter)

if (-not (Test-Path $LocalDestPath)) { New-Item -ItemType Directory -Path $LocalDestPath -Force | Out-Null }
Copy-Item -Path "C:\Temp\Network_Diagnostics.zip" -Destination "$LocalDestPath\Network_Diagnostics.zip" -FromSession $Session

Invoke-Command -Session $Session -ScriptBlock { Remove-Item "C:\Temp\Network_Diagnostics.zip" -Force -ErrorAction SilentlyContinue }
Remove-PSSession $Session