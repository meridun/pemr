<#
.SYNOPSIS
    VDM diagnostics capture wrapper. Run on the PHYSICAL client (not the VM).

.DESCRIPTION
    - Snapshots adapter / link / gateway config into the log header.
    - Runs a continuous timestamped ping to the Horizon gateway in the background.
    - Foreground: press ENTER the instant the session slideshows/stalls -> writes a
      "### STALL MARKER ###" line with wall-clock time into the SAME log, so
      analyze-logs.ps1 can correlate. Optional free text before ENTER is recorded.
    - Type q + ENTER to stop cleanly.

.EXAMPLE
    .\capture-client.ps1 -Target uag.corp.example.com
    # $Target = the Horizon UAG / connection-server address from the Horizon Client
    # server list — NOT the Azure VM internal name.
#>
param(
    [Parameter(Mandatory)]
    [string]$Target,
    [string]$LogDir = (Join-Path $PSScriptRoot 'logs')
)

$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$log   = Join-Path $LogDir "ping-$($Target -replace '[^\w\.\-]','_')-$stamp.log"

function Add-LogLine {
    param([string]$Line)
    # Two writers share the file (ping job + marker loop); retry briefly on lock contention.
    for ($i = 0; $i -lt 5; $i++) {
        try { Add-Content -Path $log -Value $Line -ErrorAction Stop; return }
        catch { Start-Sleep -Milliseconds 50 }
    }
    Write-Warning "Could not write line to log: $Line"
}

# ---- header: environment snapshot ----
Add-LogLine "### VDM CLIENT CAPTURE START $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff') target=$Target host=$env:COMPUTERNAME"
Add-LogLine '### --- Get-NetAdapter (Up) ---'
Get-NetAdapter | Where-Object Status -eq 'Up' |
    Select-Object Name, InterfaceDescription, LinkSpeed, MediaType |
    Format-Table -AutoSize | Out-String -Width 300 |
    ForEach-Object { $_ -split "`r?`n" } | Where-Object { $_ } |
    ForEach-Object { Add-LogLine "### $_" }
Add-LogLine '### --- Get-NetIPConfiguration ---'
Get-NetIPConfiguration |
    Select-Object InterfaceAlias,
        @{n='Gateway'; e={ $_.IPv4DefaultGateway.NextHop }},
        @{n='DNS';     e={ ($_.DNSServer.ServerAddresses) -join ',' }} |
    Format-Table -AutoSize | Out-String -Width 300 |
    ForEach-Object { $_ -split "`r?`n" } | Where-Object { $_ } |
    ForEach-Object { Add-LogLine "### $_" }
Add-LogLine '### --- capture begins; STALL markers below are user-reported ---'

# ---- background: continuous timestamped ping ----
$job = Start-Job -ScriptBlock {
    param($t, $logPath)
    ping.exe -t $t 2>&1 | ForEach-Object {
        $line = "{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff'), $_
        for ($i = 0; $i -lt 5; $i++) {
            try { Add-Content -Path $logPath -Value $line -ErrorAction Stop; break }
            catch { Start-Sleep -Milliseconds 50 }
        }
    }
} -ArgumentList $Target, $log

Write-Host ""
Write-Host "Logging to: $log"
Write-Host "Pinging $Target continuously in the background."
Write-Host ""
Write-Host ">>> Press ENTER the moment the session slideshows/stalls (adds a STALL marker)."
Write-Host ">>> Optionally type a note first (e.g. 'keystrokes dropped'), then ENTER."
Write-Host ">>> Type q then ENTER to stop." -ForegroundColor Yellow
Write-Host ""

try {
    while ($true) {
        $note = Read-Host
        if ($note -eq 'q') { break }
        $line = "{0}  ### STALL MARKER ### {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff'), $note
        Add-LogLine $line
        Write-Host "Marked: $line" -ForegroundColor Cyan
        if ($job.State -ne 'Running') {
            Write-Warning "Ping job stopped (state: $($job.State)). Check target name / connectivity."
        }
    }
}
finally {
    Stop-Job $job -ErrorAction SilentlyContinue
    Remove-Job $job -Force -ErrorAction SilentlyContinue
    Add-LogLine "### VDM CLIENT CAPTURE END $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff')"
    Write-Host "Capture stopped. Log: $log"
}
