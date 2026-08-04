# VDM diagnostics kit

Timestamped, percentile-based evidence for the Horizon → Azure VDM lag investigation.
Averages hide micro-stalls; everything here samples at 1 s and correlates to wall-clock
STALL markers so the writeup can say *"at 10:42:15 loss hit 4% and disk latency 800 ms"*.

## Files

| File | Runs on | Purpose |
|---|---|---|
| `capture-client.ps1` | **Physical client** | Timestamped continuous ping to the Horizon gateway + user-triggered STALL markers, one log |
| `vdm-perfmon-1s.xml` | **VDM (Azure VM)** | PerfMon data-collector set: per-process CPU, disk latency/queue, memory, run queue, NIC — 1 s samples, binary log |
| `analyze-logs.ps1` | anywhere | Computes p95/p99/max with timestamps from both logs, correlates to STALL markers, emits markdown report |

## 0. Answer first (before deep analysis)

1. **Topology A vs B**: is Horizon (Blast/PCoIP) the display protocol straight to the Azure VM,
   or is there an `mstsc`/RDP hop *inside* the Horizon session? Check: inside the session, is a
   second remote-desktop window/client involved?
2. **Wired or WiFi** on the client? (`capture-client.ps1` logs this in its header.)
3. **Blast/PCoIP on UDP or TCP?** VM-side: `netstat -ano | findstr 22443` — TCP-only 22443
   sessions with no UDP peer means UDP is blocked and the protocol fell back to TCP
   (head-of-line blocking on a lossy WAN = both symptoms).
4. Azure: VM SKU, Accelerated Networking on/off, disk tier + IOPS/throughput caps.

## 1. Client side (physical workstation)

```powershell
# one-time: path survey to the Horizon UAG/connection-server (from Horizon Client server list)
pathping -n -q 50 <horizon-gateway>

# during a work session:
.\capture-client.ps1 -Target <horizon-gateway>
# Press ENTER (optionally with a note) the instant it slideshows -> STALL marker.
# q + ENTER to stop. Log lands in .\logs\.
```

Also watch client CPU during a stall — pegged decoder = client-side decode problem
(slideshow with a perfectly healthy VM).

## 2. VM side (run elevated on the VDM)

```powershell
logman import -n VDM-Diag -xml .\vdm-perfmon-1s.xml
logman start VDM-Diag
# ... work through a slow period ...
logman stop VDM-Diag
# log: C:\PerfLogs\VDM-Diag\<yyyyMMdd-NNNNNN>\vdm-diag.blg
# cleanup when done: logman delete VDM-Diag
```

Binary (.blg) on purpose — CSV freezes the process-instance list at start and misses
processes that spawn later; `analyze-logs.ps1` auto-converts via `relog`.

Also enable **Horizon Performance Tracker** in the session and screenshot the overlay
(Blast RTT, FPS, encoder time, packet loss) at the moment of a stall — single best signal
for whether the display hop itself is the culprit.

## 3. Analyze

```powershell
.\analyze-logs.ps1 -PingLog .\logs\ping-<gw>-<stamp>.log `
                   -PerfmonLog C:\PerfLogs\VDM-Diag\...\vdm-diag.blg `
                   -OutFile vdm-report.md
```

Report contains: ping loss %, RTT p95/p99/max + worst samples + loss bursts; per-counter
avg/p95/p99/max with max timestamps; top-15 processes by p99 CPU; disk-latency red flags
(p99 > 50 ms ⇒ Azure disk-cap signature); and per-STALL-marker windows (±30 s, tunable via
`-StallWindowSeconds`) across both logs.

## Reading the results

- **Loss on pathping hops 1–2 / loss bursts on WiFi** → local link. Fix locally; infra is off the hook.
- **Clean to gateway but Blast RTT/loss spikes in Performance Tracker** → display-protocol path
  (UDP blocked → TCP fallback is the prime suspect).
- **Disk `Avg. Disk sec/*` p99 spikes correlated with stalls** → Azure disk IOPS/throughput cap;
  ask infra for the disk-throttle metrics (`Data Disk IOPS Consumed Percentage`) at those exact timestamps.
- **A single process's CPU p99 spiking at stall times** → the "too many logging programs" theory, now with a name.
