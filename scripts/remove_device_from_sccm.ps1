# Save as: scripts/remove_device_from_sccm.ps1
<#
.SYNOPSIS
    Removes a computer from Microsoft SCCM and optionally Active Directory in preparation for reimaging.
.DESCRIPTION
    Non-interactive automation script based on GUI-remove_device_from_sccm-v1.03.1.ps1.
    Queries the SCCM SMS Provider via WMI (SMS_R_System), deletes the ResourceID,
    writes an audit record to the central CSV log, and optionally removes the computer
    account from Active Directory. Outputs JSON for backend consumption.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory=$false)]
    [string]$DeviceName = "",

    [Parameter(Mandatory=$false)]
    [string]$MacAddress = "",

    [Parameter(Mandatory=$false)]
    [string]$OperatorId = "",

    [Parameter(Mandatory=$false)]
    [string]$RemoveSccm = "true",

    [Parameter(Mandatory=$false)]
    [string]$RemoveAd = "true",

    [Parameter(Mandatory=$false)]
    [string]$SiteServer = $(if ($env:SCCM_SITE_SERVER) { $env:SCCM_SITE_SERVER } else { "SCCM01.corp.internal" }),

    [Parameter(Mandatory=$false)]
    [string]$SiteCode = $(if ($env:SCCM_SITE_CODE) { $env:SCCM_SITE_CODE } else { "HQ1" }),

    [Parameter(Mandatory=$false)]
    [string]$LogPath = $(if ($env:SCCM_LOG_PATH) { $env:SCCM_LOG_PATH } else { "\\SCCM01.corp.internal\Asset_Removal_Logs$\Asset_Removal_Logs.csv" }),

    [Parameter(Mandatory=$false)]
    [switch]$UseStdinCredentials = $false
)

# Set execution preferences
$ErrorActionPreference = "Stop"

# Parse optional step-up credentials passed securely via stdin JSON
$cred = $null
$stdinDevices = @()
try {
    if ($UseStdinCredentials) {
        $rawStdin = [Console]::In.ReadToEnd()
        if ($rawStdin -and $rawStdin.Trim() -ne "") {
            $inputData = $rawStdin | ConvertFrom-Json
            if ($inputData.username -and $inputData.password) {
                $rawUser = $inputData.username.ToString().Trim()
                $netbiosDomain = if ($env:AD_NETBIOS_DOMAIN) { $env:AD_NETBIOS_DOMAIN } elseif ($env:USERDOMAIN) { $env:USERDOMAIN } else { "" }
                $ldapDomain = if ($env:LDAP_DOMAIN) { $env:LDAP_DOMAIN } elseif ($env:USERDNSDOMAIN) { $env:USERDNSDOMAIN.ToLower() } else { "" }
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

                if (-not $OperatorId) {
                    $OperatorId = $domainUser
                }
            }
            if ($inputData.devices) {
                foreach ($d in $inputData.devices) {
                    $dStr = "$d".Trim()
                    if ($dStr) { $stdinDevices += $dStr }
                }
            }
        }
    }
} catch {
    Write-Warning "Failed to parse data from stdin: $($_.Exception.Message)"
}

$shouldRemoveSccm = ($RemoveSccm -is [bool] -and $RemoveSccm) -or ($RemoveSccm.ToString().ToLower() -in @("true", "1", "$true"))
$shouldRemoveAd = ($RemoveAd -is [bool] -and $RemoveAd) -or ($RemoveAd.ToString().ToLower() -in @("true", "1", "$true"))

# Helper to format and write audit log to the centralized UNC share
function Write-CentralRemovalLog {
    param(
        [string]$TargetName,
        [string]$AdminUser,
        [string]$TargetLogPath,
        [string]$Status = "Success"
    )
    try {
        $logUser = if ($AdminUser -and $AdminUser.Trim() -ne "") { 
            $AdminUser.Trim() 
        } elseif ($cred) {
            $cred.UserName
        } else { 
            [System.Security.Principal.WindowsIdentity]::GetCurrent().Name 
        }

        $logEntry = [PSCustomObject]@{
            Timestamp    = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
            AdminUser    = $logUser
            ComputerName = $TargetName
            Status       = $Status
        }

        $logDir = Split-Path -Path $TargetLogPath -Parent
        if ($logDir -and -not (Test-Path $logDir)) {
            New-Item -Path $logDir -ItemType Directory -Force | Out-Null
        }

        $logEntry | Export-Csv -Path $TargetLogPath -Append -NoTypeInformation
        return $true
    } catch {
        Write-Warning "Failed to write audit log entry to $($TargetLogPath): $($_.Exception.Message)"
        return $false
    }
}

# Resolve list of target devices (either from stdin JSON array or delimited DeviceName)
$targetDevices = @()
if ($stdinDevices.Count -gt 0) {
    $targetDevices = $stdinDevices
} elseif ($DeviceName) {
    $splitItems = $DeviceName -split '[,\r\n\s]+'
    foreach ($d in $splitItems) {
        $dStr = "$d".Trim()
        if ($dStr) { $targetDevices += $dStr }
    }
}

# -----------------------------------------------------------------------------
# BULK PROCESSING MODE (When more than 1 device is specified)
# -----------------------------------------------------------------------------
if ($targetDevices.Count -gt 1) {
    $hasAdModule = $false
    if ($shouldRemoveAd) {
        try {
            if (Get-Module -ListAvailable ActiveDirectory) {
                Import-Module ActiveDirectory -ErrorAction Stop
                $hasAdModule = $true
            }
        } catch {
            $hasAdModule = $false
        }
    }

    $adDe = $null
    if ($shouldRemoveAd -and -not $hasAdModule -and $cred) {
        try {
            $adDomain = [System.DirectoryServices.ActiveDirectory.Domain]::GetCurrentDomain().Name
            $adDe = New-Object System.DirectoryServices.DirectoryEntry("LDAP://$adDomain", $cred.UserName, $cred.GetNetworkCredential().Password)
        } catch {
            Write-Warning "Failed to pre-bind DirectoryEntry: $($_.Exception.Message)"
        }
    }

    $wmiNamespace = "root\sms\site_$SiteCode"
    $deviceResults = @()
    $sccmRemovedCount = 0
    $sccmNotFoundCount = 0
    $adRemovedCount = 0
    $adNotFoundCount = 0
    $failedCount = 0

    foreach ($targetDev in $targetDevices) {
        $devRes = [ordered]@{
            device_name   = $targetDev
            resolved_mac  = ""
            success       = $false
            sccm          = [ordered]@{
                attempted     = $shouldRemoveSccm
                found         = $false
                removed       = $false
                resource_id   = $null
                mac_addresses = @()
                log_written   = $false
                message       = ""
            }
            ad            = [ordered]@{
                attempted     = $shouldRemoveAd
                found         = $false
                removed       = $false
                message       = ""
            }
        }

        # 1. SCCM Removal
        if ($shouldRemoveSccm) {
            try {
                $query = "SELECT ResourceID, Name, MACAddresses FROM SMS_R_System WHERE Name = '$targetDev'"
                $resources = if ($cred) {
                    Get-WmiObject -ComputerName $SiteServer -Namespace $wmiNamespace -Query $query -Credential $cred -ErrorAction Stop
                } else {
                    Get-WmiObject -ComputerName $SiteServer -Namespace $wmiNamespace -Query $query -ErrorAction Stop
                }

                if ($resources) {
                    $targetResource = if ($resources -is [array]) { $resources[0] } else { $resources }
                    $resourceId = $targetResource.ResourceID
                    $matchedName = $targetResource.Name

                    $matchedMacs = @()
                    if ($targetResource.MACAddresses) {
                        foreach ($m in $targetResource.MACAddresses) {
                            if ($m) {
                                $macStr = $m.ToString().Trim().Replace("-", ":").ToUpper()
                                if ($macStr.Length -eq 12 -and -not ($macStr -match ":")) {
                                    $macStr = ($macStr -split '(?<=\G.{2})(?!$)' -join ':')
                                }
                                if ($macStr -ne "") {
                                    $matchedMacs += $macStr
                                }
                            }
                        }
                    }

                    $devRes.sccm.found = $true
                    $devRes.sccm.resource_id = $resourceId
                    $devRes.sccm.mac_addresses = @($matchedMacs)
                    if ($matchedMacs.Count -gt 0) {
                        $devRes.resolved_mac = [string]$matchedMacs[0]
                    }

                    # Delete from SCCM
                    if ($cred) {
                        try {
                            $targetResource.psbase.Delete()
                        } catch {
                            Remove-WmiObject -Path "\\$SiteServer\$wmiNamespace:SMS_R_System.ResourceID='$resourceId'" -Credential $cred -ErrorAction Stop
                        }
                    } else {
                        try {
                            $targetResource.psbase.Delete()
                        } catch {
                            $systemObj = [WMI]"\\$SiteServer\$wmiNamespace:SMS_R_System.ResourceID='$resourceId'"
                            $systemObj.psbase.Delete()
                        }
                    }

                    $devRes.sccm.removed = $true
                    $devRes.sccm.message = "Successfully removed device '$matchedName' (ResourceID: $resourceId) from SCCM."
                    $sccmRemovedCount++

                    $logOk = Write-CentralRemovalLog -TargetName $matchedName -AdminUser $OperatorId -TargetLogPath $LogPath -Status "Success"
                    $devRes.sccm.log_written = $logOk
                } else {
                    $devRes.sccm.found = $false
                    $devRes.sccm.removed = $false
                    $devRes.sccm.message = "Device '$targetDev' not found in SCCM site $SiteCode."
                    $sccmNotFoundCount++
                }
            } catch {
                $devRes.sccm.found = $false
                $devRes.sccm.removed = $false
                $devRes.sccm.error = $true
                $devRes.sccm.message = "SCCM Error: $($_.Exception.Message)"
            }
        }

        # 2. AD Removal
        if ($shouldRemoveAd) {
            try {
                if ($hasAdModule) {
                    $adComp = if ($cred) {
                        Get-ADComputer -Filter "Name -eq '$targetDev'" -Credential $cred -ErrorAction SilentlyContinue
                    } else {
                        Get-ADComputer -Filter "Name -eq '$targetDev'" -ErrorAction SilentlyContinue
                    }
                    if ($adComp) {
                        $devRes.ad.found = $true
                        if ($cred) {
                            Remove-ADComputer -Identity $adComp.DistinguishedName -Confirm:$false -Credential $cred -ErrorAction Stop
                        } else {
                            Remove-ADComputer -Identity $adComp.DistinguishedName -Confirm:$false -ErrorAction Stop
                        }
                        $devRes.ad.removed = $true
                        $devRes.ad.message = "Successfully deleted computer account '$targetDev' from Active Directory."
                        $adRemovedCount++
                    } else {
                        $devRes.ad.found = $false
                        $devRes.ad.removed = $false
                        $devRes.ad.message = "Computer account '$targetDev' not found in Active Directory."
                        $adNotFoundCount++
                    }
                } else {
                    $searcher = if ($adDe) {
                        New-Object System.DirectoryServices.DirectorySearcher($adDe)
                    } elseif ($cred) {
                        $dom = [System.DirectoryServices.ActiveDirectory.Domain]::GetCurrentDomain().Name
                        $entry = New-Object System.DirectoryServices.DirectoryEntry("LDAP://$dom", $cred.UserName, $cred.GetNetworkCredential().Password)
                        New-Object System.DirectoryServices.DirectorySearcher($entry)
                    } else {
                        [adsisearcher]""
                    }
                    $searcher.Filter = "(&(objectCategory=computer)(cn=$targetDev))"
                    $found = $searcher.FindOne()
                    if ($found) {
                        $devRes.ad.found = $true
                        $compEntry = if ($cred) {
                            New-Object System.DirectoryServices.DirectoryEntry($found.Path, $cred.UserName, $cred.GetNetworkCredential().Password)
                        } else {
                            [ADSI]$found.Path
                        }
                        $compEntry.DeleteObject(0)
                        $devRes.ad.removed = $true
                        $devRes.ad.message = "Successfully deleted computer account '$targetDev' from Active Directory (ADSI)."
                        $adRemovedCount++
                    } else {
                        $devRes.ad.found = $false
                        $devRes.ad.removed = $false
                        $devRes.ad.message = "Computer account '$targetDev' not found in Active Directory."
                        $adNotFoundCount++
                    }
                }
            } catch {
                $devRes.ad.removed = $false
                $devRes.ad.error = $true
                $devRes.ad.message = "Active Directory Error: $($_.Exception.Message)"
            }
        }

        $sOk = (-not $devRes.sccm.attempted) -or $devRes.sccm.removed -or (-not $devRes.sccm.found -and -not $devRes.sccm.error)
        $aOk = (-not $devRes.ad.attempted) -or $devRes.ad.removed -or (-not $devRes.ad.found -and -not $devRes.ad.error)
        $devRes.success = ($sOk -and $aOk)
        if (-not $devRes.success) {
            $failedCount++
        }

        $deviceResults += $devRes
    }

    $bulkResult = [ordered]@{
        success     = ($failedCount -eq 0)
        is_bulk     = $true
        total       = $targetDevices.Count
        summary     = [ordered]@{
            total           = $targetDevices.Count
            sccm_removed    = $sccmRemovedCount
            sccm_not_found  = $sccmNotFoundCount
            ad_removed      = $adRemovedCount
            ad_not_found    = $adNotFoundCount
            failed          = $failedCount
        }
        devices     = @($deviceResults)
        operator_id = $OperatorId
        timestamp   = (Get-Date -Format "yyyy-MM-dd HH:mm:ss")
    }

    $resultJson = $bulkResult | ConvertTo-Json -Depth 5 -Compress
    Write-Output $resultJson
    exit 0
}

# If exactly 1 device was identified, assign to $DeviceName
if ($targetDevices.Count -eq 1) {
    $DeviceName = $targetDevices[0]
}

# -----------------------------------------------------------------------------
# SINGLE DEVICE EXECUTION MODE (Backward compatible)
# -----------------------------------------------------------------------------
# Result structure
$result = [ordered]@{
    success       = $false
    sccm          = [ordered]@{
        attempted     = $false
        found         = $false
        removed       = $false
        resource_id   = $null
        device_name   = $DeviceName
        mac_addresses = @()
        log_written   = $false
        message       = ""
    }
    ad            = [ordered]@{
        attempted     = $false
        found         = $false
        removed       = $false
        device_name   = $DeviceName
        message       = ""
    }
    resolved_mac  = ""
    operator_id   = $OperatorId
    timestamp     = (Get-Date -Format "yyyy-MM-dd HH:mm:ss")
}

# Format clean MAC if supplied
$cleanMac = ""
if ($MacAddress -and $MacAddress.Trim() -ne "") {
    $cleanMac = $MacAddress.Trim().Replace("-", ":").ToUpper()
    if ($cleanMac.Length -eq 12 -and -not ($cleanMac -match ":")) {
        $cleanMac = ($cleanMac -split '(?<=\G.{2})(?!$)' -join ':')
    }
    $result.resolved_mac = $cleanMac
}

$trimmedDevice = if ($DeviceName) { $DeviceName.Trim() } else { "" }

# -----------------------------------------------------------------------------
# 1. SCCM Removal
# -----------------------------------------------------------------------------
if ($shouldRemoveSccm) {
    $result.sccm.attempted = $true
    
    if (-not $trimmedDevice -and -not $cleanMac) {
        $result.sccm.message = "Neither DeviceName nor MacAddress was provided for SCCM lookup."
    } else {
        try {
            $wmiNamespace = "root\sms\site_$SiteCode"
            $query = ""

            if ($trimmedDevice -and $cleanMac) {
                $query = "SELECT ResourceID, Name, MACAddresses FROM SMS_R_System WHERE Name = '$trimmedDevice' OR MACAddresses = '$cleanMac'"
            } elseif ($trimmedDevice) {
                $query = "SELECT ResourceID, Name, MACAddresses FROM SMS_R_System WHERE Name = '$trimmedDevice'"
            } else {
                $query = "SELECT ResourceID, Name, MACAddresses FROM SMS_R_System WHERE MACAddresses = '$cleanMac'"
            }

            $resources = if ($cred) {
                Get-WmiObject -ComputerName $SiteServer -Namespace $wmiNamespace -Query $query -Credential $cred -ErrorAction Stop
            } else {
                Get-WmiObject -ComputerName $SiteServer -Namespace $wmiNamespace -Query $query -ErrorAction Stop
            }
            
            if ($resources) {
                # Pick the first matching resource record
                $targetResource = if ($resources -is [array]) { $resources[0] } else { $resources }
                $resourceId = $targetResource.ResourceID
                $matchedName = $targetResource.Name

                # Safely normalize all MAC addresses from SCCM into an array of formatted strings
                $matchedMacs = @()
                if ($targetResource.MACAddresses) {
                    foreach ($m in $targetResource.MACAddresses) {
                        if ($m) {
                            $macStr = $m.ToString().Trim().Replace("-", ":").ToUpper()
                            if ($macStr.Length -eq 12 -and -not ($macStr -match ":")) {
                                $macStr = ($macStr -split '(?<=\G.{2})(?!$)' -join ':')
                            }
                            if ($macStr -ne "") {
                                $matchedMacs += $macStr
                            }
                        }
                    }
                }

                $result.sccm.found = $true
                $result.sccm.resource_id = $resourceId
                $result.sccm.device_name = $matchedName
                $result.sccm.mac_addresses = @($matchedMacs)

                # If caller didn't provide a MAC, adopt the first MAC found in SCCM
                if (-not $result.resolved_mac -and $matchedMacs.Count -gt 0) {
                    $result.resolved_mac = [string]$matchedMacs[0]
                }

                # Delete system from SCCM SMS Provider
                if ($cred) {
                    try {
                        $targetResource.psbase.Delete()
                    } catch {
                        Remove-WmiObject -Path "\\$SiteServer\$wmiNamespace:SMS_R_System.ResourceID='$resourceId'" -Credential $cred -ErrorAction Stop
                    }
                } else {
                    try {
                        $targetResource.psbase.Delete()
                    } catch {
                        $systemObj = [WMI]"\\$SiteServer\$wmiNamespace:SMS_R_System.ResourceID='$resourceId'"
                        $systemObj.psbase.Delete()
                    }
                }

                $result.sccm.removed = $true
                $result.sccm.message = "Successfully removed device '$matchedName' (ResourceID: $resourceId) from SCCM."

                # Append to central removal CSV log
                $logSuccess = Write-CentralRemovalLog -TargetName $matchedName -AdminUser $OperatorId -TargetLogPath $LogPath -Status "Success"
                $result.sccm.log_written = $logSuccess

                # Update trimmedDevice for AD step if it wasn't supplied
                if (-not $trimmedDevice -and $matchedName) {
                    $trimmedDevice = $matchedName
                    $result.ad.device_name = $matchedName
                }
            } else {
                $result.sccm.found = $false
                $result.sccm.removed = $false
                $result.sccm.message = "Device not found in SCCM site $SiteCode on $SiteServer."
            }
        } catch {
            $result.sccm.found = $false
            $result.sccm.removed = $false
            $result.sccm.error = $true
            $result.sccm.message = "SCCM WMI Error: $($_.Exception.Message)"
        }
    }
}

# -----------------------------------------------------------------------------
# 2. Active Directory Removal
# -----------------------------------------------------------------------------
if ($shouldRemoveAd) {
    $result.ad.attempted = $true

    if (-not $trimmedDevice) {
        $result.ad.message = "Device name is required to locate and remove computer account from Active Directory."
    } else {
        try {
            if (Get-Module -ListAvailable ActiveDirectory) {
                Import-Module ActiveDirectory -ErrorAction Stop
                $adComp = if ($cred) {
                    Get-ADComputer -Filter "Name -eq '$trimmedDevice'" -Credential $cred -ErrorAction SilentlyContinue
                } else {
                    Get-ADComputer -Filter "Name -eq '$trimmedDevice'" -ErrorAction SilentlyContinue
                }
                if ($adComp) {
                    $result.ad.found = $true
                    if ($cred) {
                        Remove-ADComputer -Identity $adComp.DistinguishedName -Confirm:$false -Credential $cred -ErrorAction Stop
                    } else {
                        Remove-ADComputer -Identity $adComp.DistinguishedName -Confirm:$false -ErrorAction Stop
                    }
                    $result.ad.removed = $true
                    $result.ad.message = "Successfully deleted computer account '$trimmedDevice' from Active Directory."
                } else {
                    $result.ad.found = $false
                    $result.ad.removed = $false
                    $result.ad.message = "Computer account '$trimmedDevice' was not found in Active Directory."
                }
            } else {
                # Fallback to ADSI DirectorySearcher
                $searcher = if ($cred) {
                    $domain = [System.DirectoryServices.ActiveDirectory.Domain]::GetCurrentDomain().Name
                    $de = New-Object System.DirectoryServices.DirectoryEntry("LDAP://$domain", $cred.UserName, $cred.GetNetworkCredential().Password)
                    New-Object System.DirectoryServices.DirectorySearcher($de)
                } else {
                    [adsisearcher]""
                }
                $searcher.Filter = "(&(objectCategory=computer)(cn=$trimmedDevice))"
                $found = $searcher.FindOne()
                if ($found) {
                    $result.ad.found = $true
                    $compEntry = if ($cred) {
                        New-Object System.DirectoryServices.DirectoryEntry($found.Path, $cred.UserName, $cred.GetNetworkCredential().Password)
                    } else {
                        [ADSI]$found.Path
                    }
                    $compEntry.DeleteObject(0)
                    $result.ad.removed = $true
                    $result.ad.message = "Successfully deleted computer account '$trimmedDevice' from Active Directory (ADSI)."
                } else {
                    $result.ad.found = $false
                    $result.ad.removed = $false
                    $result.ad.message = "Computer account '$trimmedDevice' was not found in Active Directory."
                }
            }
        } catch {
            $result.ad.removed = $false
            $result.ad.error = $true
            $result.ad.message = "Active Directory Error: $($_.Exception.Message)"
        }
    }
}

# Overall success flag: True if at least one attempted operation succeeded without fatal failure
$sccmOk = (-not $result.sccm.attempted) -or $result.sccm.removed -or (-not $result.sccm.found -and -not $result.sccm.error)
$adOk = (-not $result.ad.attempted) -or $result.ad.removed -or (-not $result.ad.found -and -not $result.ad.error)
$result.success = $sccmOk -and $adOk

# Output compact JSON for Python caller
$resultJson = $result | ConvertTo-Json -Depth 4 -Compress
Write-Output $resultJson
