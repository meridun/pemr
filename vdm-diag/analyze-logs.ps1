<#
.SYNOPSIS
    Compute max / p99 / p95 (with timestamps) from capture-client.ps1 ping logs and
    the VDM-Diag PerfMon log, correlated to STALL markers. Output is a markdown
    report ready to paste into the infra writeup.

.EXAMPLE
    .\analyze-logs.ps1 -PingLog .\logs\ping-uag-20260716-090000.log
    .\analyze-logs.ps1 -PerfmonLog C:\PerfLogs\VDM-Diag\...\vdm-diag.blg
    .\analyze-logs.ps1 -PingLog <log> -PerfmonLog <blg-or-csv> -OutFile report.md
#>
param(
    [string]$PingLog,
    [string]$PerfmonLog,                 # .blg (auto-converted via relog) or .csv
    [int]$StallWindowSeconds = 30,       # +/- window around each STALL marker
    [string]$OutFile
)

$ErrorActionPreference = 'Stop'
$inv = [System.Globalization.CultureInfo]::InvariantCulture
if (-not $PingLog -and -not $PerfmonLog) { throw 'Provide -PingLog and/or -PerfmonLog.' }

function Get-Percentile {
    param([double[]]$Sorted, [double]$P)   # $Sorted must be ascending
    if (-not $Sorted -or $Sorted.Count -eq 0) { return $null }
    $i = [math]::Ceiling($P / 100 * $Sorted.Count) - 1
    $Sorted[[math]::Max(0, [math]::Min($i, $Sorted.Count - 1))]
}

function Format-Num { param($v) if ($null -eq $v) { 'n/a' } else { [math]::Round([double]$v, 3).ToString($inv) } }

$report = [System.Collections.Generic.List[string]]::new()
function Out-Line { param([string]$s = '') $report.Add($s) }

Out-Line "# VDM diagnostics report"
Out-Line "Generated: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  |  Stall window: +/-${StallWindowSeconds}s"
Out-Line ''

$markers = @()

# ================= PING LOG =================
if ($PingLog) {
    $pings = [System.Collections.Generic.List[object]]::new()
    foreach ($line in [System.IO.File]::ReadLines((Resolve-Path $PingLog))) {
        if ($line -notmatch '^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\s+(.*)$') { continue }
        $ts   = [datetime]::ParseExact($Matches[1], 'yyyy-MM-dd HH:mm:ss.fff', $inv)
        $rest = $Matches[2]
        if ($rest -match '###\s*STALL MARKER\s*###\s*(.*)$') {
            $markers += [pscustomobject]@{ Time = $ts; Note = $Matches[1].Trim() }
        }
        elseif ($rest -match 'time[=<](\d+)\s*ms') {
            $pings.Add([pscustomobject]@{ Time = $ts; Ms = [double]$Matches[1]; Lost = $false })
        }
        elseif ($rest -match 'timed out|unreachable|General failure|transmit failed|could not find host') {
            $pings.Add([pscustomobject]@{ Time = $ts; Ms = $null; Lost = $true })
        }
    }

    Out-Line "## Ping / loss ($([System.IO.Path]::GetFileName($PingLog)))"
    if ($pings.Count -eq 0) {
        Out-Line '_No ping samples parsed — check the log format._'
    }
    else {
        $lost   = @($pings | Where-Object Lost)
        $ok     = @($pings | Where-Object { -not $_.Lost })
        $sorted = [double[]]@($ok.Ms | Sort-Object)
        $span   = New-TimeSpan $pings[0].Time $pings[-1].Time
        Out-Line "Window: $($pings[0].Time.ToString('yyyy-MM-dd HH:mm:ss')) -> $($pings[-1].Time.ToString('HH:mm:ss')) ($([int]$span.TotalMinutes) min), $($pings.Count) samples"
        Out-Line ''
        Out-Line '| Metric | Value |'
        Out-Line '|---|---|'
        Out-Line "| Packet loss | $($lost.Count) / $($pings.Count) = $([math]::Round(100.0 * $lost.Count / $pings.Count, 2))% |"
        Out-Line "| RTT min / median | $(Format-Num ($sorted | Select-Object -First 1)) ms / $(Format-Num (Get-Percentile $sorted 50)) ms |"
        Out-Line "| RTT p95 | $(Format-Num (Get-Percentile $sorted 95)) ms |"
        Out-Line "| RTT p99 | $(Format-Num (Get-Percentile $sorted 99)) ms |"
        Out-Line "| RTT max | $(Format-Num (Get-Percentile $sorted 100)) ms |"
        Out-Line ''

        $worst = $ok | Sort-Object Ms -Descending | Select-Object -First 10
        if ($worst) {
            Out-Line '**Worst RTT samples:**'
            foreach ($w in $worst) { Out-Line "- $($w.Time.ToString('HH:mm:ss.fff'))  $($w.Ms) ms" }
            Out-Line ''
        }

        # loss bursts: runs of >=2 consecutive lost samples
        $bursts = @(); $run = @()
        foreach ($p in $pings) {
            if ($p.Lost) { $run += $p }
            else { if ($run.Count -ge 2) { $bursts += ,$run }; $run = @() }
        }
        if ($run.Count -ge 2) { $bursts += ,$run }
        if ($bursts.Count) {
            Out-Line "**Loss bursts (>=2 consecutive):**"
            foreach ($b in $bursts) { Out-Line "- $($b[0].Time.ToString('HH:mm:ss.fff'))  x$($b.Count) consecutive losses" }
        } else {
            Out-Line '_No multi-packet loss bursts._'
        }
        Out-Line ''

        if ($markers.Count) {
            Out-Line "## Stall markers vs ping (+/-${StallWindowSeconds}s)"
            Out-Line ''
            Out-Line '| Stall time | Note | Loss in window | p95 RTT | max RTT | Baseline p95 (whole run) |'
            Out-Line '|---|---|---|---|---|---|'
            $basP95 = Get-Percentile $sorted 95
            foreach ($m in $markers) {
                $w    = @($pings | Where-Object { [math]::Abs(($_.Time - $m.Time).TotalSeconds) -le $StallWindowSeconds })
                $wOk  = [double[]]@(($w | Where-Object { -not $_.Lost }).Ms | Sort-Object)
                $wLost = @($w | Where-Object Lost).Count
                $lossPct = if ($w.Count) { [math]::Round(100.0 * $wLost / $w.Count, 1) } else { 0 }
                Out-Line "| $($m.Time.ToString('HH:mm:ss')) | $($m.Note) | $wLost/$($w.Count) ($lossPct%) | $(Format-Num (Get-Percentile $wOk 95)) ms | $(Format-Num (Get-Percentile $wOk 100)) ms | $(Format-Num $basP95) ms |"
            }
            Out-Line ''
        } else {
            Out-Line '_No STALL markers found in the ping log._'
            Out-Line ''
        }
    }
}

# ================= PERFMON LOG =================
if ($PerfmonLog) {
    $csvPath = $PerfmonLog
    if ([System.IO.Path]::GetExtension($PerfmonLog) -ieq '.blg') {
        $csvPath = Join-Path ([System.IO.Path]::GetTempPath()) ("vdm-diag-{0}.csv" -f (Get-Date -Format 'yyyyMMddHHmmss'))
        Write-Host "Converting $PerfmonLog -> $csvPath via relog..."
        & relog.exe $PerfmonLog -f csv -o $csvPath -y | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "relog failed (exit $LASTEXITCODE). Run: relog `"$PerfmonLog`" -f csv -o out.csv" }
    }

    $rows = Import-Csv $csvPath
    if (-not $rows) { throw "No rows in $csvPath" }
    $cols = @($rows[0].PSObject.Properties.Name)
    $tsCol = $cols[0]   # "(PDH-CSV 4.0) (...)"
    $counterCols = $cols | Select-Object -Skip 1

    # parse per-row timestamps once
    $times = foreach ($r in $rows) {
        try { [datetime]::ParseExact($r.$tsCol, 'MM/dd/yyyy HH:mm:ss.fff', $inv) } catch { $null }
    }

    function Get-ColStats {
        param([string]$Col)
        $vals = [System.Collections.Generic.List[object]]::new()
        for ($i = 0; $i -lt $rows.Count; $i++) {
            $raw = $rows[$i].$Col
            $d = 0.0
            if ($raw -and [double]::TryParse($raw, [System.Globalization.NumberStyles]::Float, $inv, [ref]$d)) {
                $vals.Add([pscustomobject]@{ Time = $times[$i]; V = $d })
            }
        }
        if ($vals.Count -eq 0) { return $null }
        $sorted = [double[]]@($vals.V | Sort-Object)
        $maxRow = $vals | Sort-Object V -Descending | Select-Object -First 1
        [pscustomobject]@{
            Counter = $Col; N = $vals.Count
            Avg = ($vals.V | Measure-Object -Average).Average
            P95 = Get-Percentile $sorted 95
            P99 = Get-Percentile $sorted 99
            Max = $maxRow.V
            MaxAt = $maxRow.Time
            Values = $vals
        }
    }

    function Short-Name { param([string]$c) ($c -replace '^\\\\[^\\]+', '') }  # strip \\HOST prefix

    Out-Line "## PerfMon ($([System.IO.Path]::GetFileName($PerfmonLog)))"
    Out-Line ''

    $procCpuCols  = @($counterCols | Where-Object { $_ -match '\\Process\('       -and $_ -match '% Processor Time' -and $_ -notmatch '\((_Total|Idle)\)' })
    $systemCols   = @($counterCols | Where-Object { $_ -notin $procCpuCols })

    Out-Line '### System counters (avg / p95 / p99 / max @ time)'
    Out-Line ''
    Out-Line '| Counter | avg | p95 | p99 | max | max at |'
    Out-Line '|---|---|---|---|---|---|'
    $sysStats = foreach ($c in $systemCols) { Get-ColStats $c }
    $sysStats = @($sysStats | Where-Object { $_ })
    foreach ($s in $sysStats) {
        Out-Line "| $(Short-Name $s.Counter) | $(Format-Num $s.Avg) | $(Format-Num $s.P95) | $(Format-Num $s.P99) | $(Format-Num $s.Max) | $($s.MaxAt.ToString('HH:mm:ss')) |"
    }
    Out-Line ''

    if ($procCpuCols.Count) {
        Out-Line '### Top processes by p99 CPU (%; catches EDR/logging/SCCM spikes)'
        Out-Line ''
        Out-Line '| Process | avg | p95 | p99 | max | max at |'
        Out-Line '|---|---|---|---|---|---|'
        $procStats = @(foreach ($c in $procCpuCols) { Get-ColStats $c } ) | Where-Object { $_ } |
            Sort-Object P99 -Descending | Select-Object -First 15
        foreach ($s in $procStats) {
            $name = if ($s.Counter -match '\\Process\(([^)]+)\)') { $Matches[1] } else { $s.Counter }
            Out-Line "| $name | $(Format-Num $s.Avg) | $(Format-Num $s.P95) | $(Format-Num $s.P99) | $(Format-Num $s.Max) | $($s.MaxAt.ToString('HH:mm:ss')) |"
        }
        Out-Line ''
    }

    # Disk latency red-flag: any Avg. Disk sec/* p99 above 50 ms is a throttling signature
    $diskLat = $sysStats | Where-Object { $_.Counter -match 'Avg\. Disk sec/' -and $_.P99 -gt 0.05 }
    foreach ($d in $diskLat) {
        Out-Line ("**RED FLAG:** $(Short-Name $d.Counter) p99 = $([math]::Round($d.P99*1000,1)) ms " +
                  "(max $([math]::Round($d.Max*1000,1)) ms @ $($d.MaxAt.ToString('HH:mm:ss'))) — " +
                  "consistent with Azure disk IOPS/throughput cap throttling.")
    }
    if ($diskLat) { Out-Line '' }

    # correlate with stall markers from the ping log
    if ($markers.Count) {
        Out-Line "### Stall markers vs PerfMon (+/-${StallWindowSeconds}s window maxima)"
        Out-Line ''
        foreach ($m in $markers) {
            Out-Line "**$($m.Time.ToString('HH:mm:ss')) $($m.Note)**"
            $hits = foreach ($s in $sysStats) {
                $w = @($s.Values | Where-Object { $_.Time -and [math]::Abs(($_.Time - $m.Time).TotalSeconds) -le $StallWindowSeconds })
                if ($w) {
                    $wm = $w | Sort-Object V -Descending | Select-Object -First 1
                    [pscustomobject]@{ C = (Short-Name $s.Counter); Max = $wm.V; At = $wm.Time; RunP95 = $s.P95 }
                }
            }
            foreach ($h in ($hits | Sort-Object { if ($_.RunP95) { $_.Max / [math]::Max($_.RunP95, 1e-9) } else { 0 } } -Descending | Select-Object -First 8)) {
                Out-Line "- $($h.C): window max $(Format-Num $h.Max) @ $($h.At.ToString('HH:mm:ss')) (run p95 $(Format-Num $h.RunP95))"
            }
            Out-Line ''
        }
    }
}

$text = $report -join [Environment]::NewLine
if ($OutFile) {
    Set-Content -Path $OutFile -Value $text -Encoding UTF8
    Write-Host "Report written to $OutFile"
}
$text
