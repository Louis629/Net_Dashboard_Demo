Add-Type -AssemblyName PresentationFramework

# Configuration Variables
$SiteServer = if ($env:SCCM_SITE_SERVER) { $env:SCCM_SITE_SERVER } else { "SCCM01.corp.internal" } # Set your SCCM Site Server
$SiteCode = if ($env:SCCM_SITE_CODE) { $env:SCCM_SITE_CODE } else { "HQ1" } # Set your SCCM Site Code
$IconPath = if ($env:SCCM_GUI_ICON_PATH) { $env:SCCM_GUI_ICON_PATH } else { "" } # Set path to your icon, or leave empty
$LogPath   = if ($env:SCCM_LOG_PATH) { $env:SCCM_LOG_PATH } else { "\\SCCM01.corp.internal\Asset_Removal_Logs$\Asset_Removal_Logs.csv" } # Path to your central CSV log

# --- NEW LOGGING FUNCTION ---
function Write-RemovalLog {
    param (
        [Parameter(Mandatory=$true)]
        [string]$DeviceName
    )
    
    $LogEntry = [PSCustomObject]@{
        Timestamp    = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
        AdminUser    = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
        ComputerName = $DeviceName
        Status       = "Success"
    }

    # Create CSV with headers if it doesn't exist, otherwise append
    $LogEntry | Export-Csv -Path $LogPath -Append -NoTypeInformation
}
# ----------------------------

[xml]$xaml = @"
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="SCCM Device Removal Tool" Height="180" Width="400" SizeToContent="Height" ResizeMode="CanResize">
    <Grid>
        <Grid.RowDefinitions>
            <RowDefinition Height="30"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
        </Grid.RowDefinitions>
        <Rectangle Grid.Row="0" Fill="#0091FF" />
        <StackPanel Grid.Row="0" Orientation="Horizontal" VerticalAlignment="Center" Margin="10,0">
            <Image Name="imgIcon" Width="16" Height="16" Margin="0,0,5,0" />
            <TextBlock Text="Remove Device From SCCM - v1.03.1" Foreground="White" VerticalAlignment="Center" FontWeight="Bold"/>
        </StackPanel>
        <StackPanel Grid.Row="1" Margin="10">
            <TextBlock Text="Device Name:" Margin="0,5,0,0"/>
            <TextBox Name="txtDeviceName" Margin="10,2" Height="80" AcceptsReturn="True" TextWrapping="Wrap" />
        </StackPanel>
        <Button Name="btnSubmit" Content="Submit" HorizontalAlignment="Right" Width="75" Height="25" Margin="10" Grid.Row="2"/>
    </Grid>
</Window>
"@

$reader = (New-Object System.Xml.XmlNodeReader $xaml)
$window = [Windows.Markup.XamlReader]::Load($reader)

$txtDeviceName = $window.FindName("txtDeviceName")
$btnSubmit = $window.FindName("btnSubmit")
$imgIcon = $window.FindName("imgIcon")

if ($IconPath -and (Test-Path $IconPath)) {
    $imgIcon.Source = $IconPath
    $window.Icon = $IconPath
}

$btnSubmit.Add_Click({
    $InputText = $txtDeviceName.Text.Trim()
    if ($InputText) {
        $DeviceNames = $InputText -split '[,\r\n]+' | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne '' }
        $RemovedCount = 0
        $NotFound = @()
        
        foreach ($DeviceName in $DeviceNames) {
            try {
                $ResourceID = (Get-WmiObject -ComputerName $SiteServer -Namespace "root\sms\site_$SiteCode" -Query "SELECT ResourceID FROM SMS_R_System WHERE Name = '$DeviceName'").ResourceID
                if ($ResourceID) {
                    $System = [WMI]"\\$SiteServer\root\sms\site_$($SiteCode):SMS_R_System.ResourceID='$ResourceID'"
                    $System.psbase.Delete()
                    
                    # Log the successful removal
                    Write-RemovalLog -DeviceName $DeviceName
                    
                    $RemovedCount++
                } else {
                    $NotFound += $DeviceName
                }
            }
            catch {
                $NotFound += $DeviceName
            }
        }
        
        $Message = "Successfully removed $RemovedCount device(s)."
        if ($NotFound.Count -gt 0) {
            $Message += "`n`nNot found or error: $($NotFound -join ', ')"
        }
        [System.Windows.MessageBox]::Show($Message, "Complete", "OK", "Information")
        $txtDeviceName.Text = ""
    } else {
        [System.Windows.MessageBox]::Show("Please enter at least one device name.", "Input Required", "OK", "Warning")
    }
})

$window.ShowDialog() | Out-Null