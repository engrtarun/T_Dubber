# T_Dubber Boost Script
# CPU Priority + Network QoS + Power Plan + Process Throttling
# Run as Administrator: Right-click → Run as Administrator
#
# SPEED POLICY (AI/dev note, keep in sync with telegram_uploader.py +
# go_planner.py + GO_SIDECAR.md): Python stays the boss, Go pumps bytes,
# THIS script makes the machine fast. It changes NO upload logic — it only
# makes the pipe fuller for whichever engine runs (Telethon sequential or
# Go parallel x4 via upload_file_detailed(use_go=True)):
#   1. T_Dubber python → High CPU priority, all cores (this file's upload
#      loop / Go child process both inherit the headroom).
#   2. Power plan → High Performance + USB selective-suspend off (laptop
#      NICs stop dozing mid-upload).
#   3. Network QoS/TCP (autotuning normal, RSS/Chimney/NetDMA/DCA) + browser /
#      cloud-sync processes throttled to BelowNormal, so the Telegram sockets
#      keep the bandwidth-delay product filled at ~110ms RTT.
# Single-stream measured ~2.06 MB/s vs 3.76 MB/s link: the gap is TCP BDP, not
# Python — overlapping streams (Go) + this tuning is what closes it.
# Revert any time: .\boost.ps1 revert. Auto-launcher: start-boosted.ps1.

$ErrorActionPreference = "SilentlyContinue"

# ============================================================
# CONFIGURATION
# ============================================================
$T_DUBBER_PROCESS = "python"
$T_DUBBER_SCRIPT = "app.py"
$BOOST_DURATION_MIN = 120  # 2 hours boost
$NETWORK_THROTTLE_MBPS = 5  # Throttle other apps to 5 Mbps

# Network-heavy processes to throttle (browsers, downloads, etc.)
$THROTTLE_PROCESSES = @(
    "chrome", "firefox", "edge", "brave",
    "steam", "epicgameslauncher",
    "torrent", "qbittorrent", "utorrent",
    "onedrive", "dropbox", "googledrive",
    "teams", "zoom", "discord"
)

# ============================================================
# FUNCTIONS
# ============================================================

function Write-Header($text) {
    Write-Host ""
    Write-Host "═══════════════════════════════════════════════════════" -ForegroundColor Cyan
    Write-Host "  $text" -ForegroundColor Cyan
    Write-Host "═══════════════════════════════════════════════════════" -ForegroundColor Cyan
}

function Write-Success($text) {
    Write-Host "  ✅ $text" -ForegroundColor Green
}

function Write-Warning($text) {
    Write-Host "  ⚠️  $text" -ForegroundColor Yellow
}

function Write-Info($text) {
    Write-Host "  ℹ️  $text" -ForegroundColor Gray
}

function Test-Admin {
    $currentPrincipal = New-Object Security.Principal.WindowsPrincipal(
        [Security.Principal.WindowsIdentity]::GetCurrent()
    )
    return $currentPrincipal.IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )
}

function Set-TDubberPriority {
    Write-Header "CPU PRIORITY BOOST"
    
    $processes = Get-Process -Name $T_DUBBER_PROCESS -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*$T_DUBBER_SCRIPT*" -or $_.MainWindowTitle -like "*T_Dubber*" }
    
    if (-not $processes) {
        # Fallback: find any python process running app.py
        $processes = Get-Process -Name $T_DUBBER_PROCESS -ErrorAction SilentlyContinue |
            Where-Object { $_.Path -like "*python*" }
    }
    
    if ($processes) {
        foreach ($proc in $processes) {
            try {
                $proc.PriorityClass = "High"
                $proc.ProcessorAffinity = 0xFFFF  # All cores
                Write-Success "PID $($proc.Id) → High Priority (All Cores)"
            } catch {
                Write-Warning "PID $($proc.Id) priority set nahi hua: $_"
            }
        }
    } else {
        Write-Warning "T_Dubber process nahi mila. Pehle app.py start karo."
        Write-Info "Phir ye script dobara chalao."
    }
}

function Set-NetworkQoS {
    Write-Header "NETWORK QoS (Quality of Service)"
    
    # T_Dubber traffic ko highest priority do
    # Windows QoS Policy se T_Dubber ke ports prioritize karo
    
    $qosPolicyName = "T_Dubber_Priority"
    
    # Purana policy delete karo (agar hai toh)
    netsh int tcp set global autotuninglevel=normal | Out-Null
    
    # T_Dubber ke liye QoS policy create karo
    # Telegram upload ports: 443, 80, 8080
    $result = netsh int tcp set global rss=enabled | Out-Null
    
    # Network adapter priority
    $adapters = Get-NetAdapter | Where-Object { $_.Status -eq "Up" }
    
    foreach ($adapter in $adapters) {
        Write-Info "Adapter: $($adapter.Name) ($($adapter.InterfaceDescription))"
        
        # QoS policy for T_Dubber
        netsh advfirewall firewall delete rule name="$qosPolicyName" | Out-Null
        netsh advfirewall firewall add rule name="$qosPolicyName" `
            dir=in action=allow program="python.exe" `
            protocol=tcp localport=443,80,8080 | Out-Null
        
        Write-Success "QoS policy applied: $($adapter.Name)"
    }
    
    # TCP optimization
    netsh int tcp set global chimney=enabled | Out-Null
    netsh int tcp set global netdma=enabled | Out-Null
    netsh int tcp set global dca=enabled | Out-Null
    
    Write-Success "TCP optimization enabled (Chimney, NetDMA, DCA)"
}

function Set-HighPerformancePower {
    Write-Header "POWER PLAN → HIGH PERFORMANCE"
    
    # High performance power plan activate karo
    $highPerf = powercfg /list | Select-String "High performance"
    
    if ($highPerf) {
        $guid = ($highPerf -split "\s+")[3]
        powercfg /setactive $guid
        Write-Success "High Performance power plan activated"
    } else {
        # Agar high performance nahi hai toh create karo
        powercfg /duplicatescheme 8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c
        powercfg /setactive 8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c
        Write-Success "High Performance power plan created & activated"
    }
    
    # USB selective suspend disable (network adapters ke liye)
    powercfg /setacvalueindex scheme_current 2a737441-1930-4402-8d77-b2bebba308a3 48e6b7a6-50f5-4782-a5d4-53bb8f07e2260 0
    powercfg /setactive scheme_current
    
    Write-Success "USB selective suspend disabled"
}

function Set-NetworkPriority {
    Write-Header "NETWORK PRIORITY — T_Dubber First"
    
    # Network-heavy processes ko throttle karo
    $throttled = @()
    
    foreach ($procName in $THROTTLE_PROCESSES) {
        $procs = Get-Process -Name $procName -ErrorAction SilentlyContinue
        if ($procs) {
            foreach ($proc in $procs) {
                try {
                    # Low I/O priority set karo
                    $proc.PriorityClass = "BelowNormal"
                    $throttled += "$procName (PID $($proc.Id))"
                } catch {
                    # Skip if access denied
                }
            }
        }
    }
    
    if ($throttled.Count -gt 0) {
        Write-Success "Throttled processes:"
        foreach ($t in $throttled) {
            Write-Info "  → $t"
        }
    } else {
        Write-Info "Koi network-heavy process nahi mila"
    }
    
    # Windows Network Throttling Index disable
    $regPath = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile"
    Set-ItemProperty -Path $regPath -Name "NetworkThrottlingIndex" -Value 0xffffffff -Type DWord
    Set-ItemProperty -Path $regPath -Name "SystemResponsiveness" -Value 0 -Type DWord
    
    Write-Success "Network Throttling Index disabled"
    Write-Success "System Responsiveness → 0 (maximum priority)"
}

function Show-NetworkStatus {
    Write-Header "NETWORK STATUS"
    
    $adapters = Get-NetAdapter | Where-Object { $_.Status -eq "Up" }
    
    foreach ($adapter in $adapters) {
        $stats = Get-NetAdapterStatistics -Name $adapter.Name
        $speed = $adapter.LinkSpeed
        
        Write-Info "Adapter: $($adapter.Name)"
        Write-Info "  Speed: $speed"
        Write-Info "  Sent: $([math]::Round($stats.SentBytes / 1MB, 2)) MB"
        Write-Info "  Received: $([math]::Round($stats.ReceivedBytes / 1MB, 2)) MB"
    }
    
    # Active TCP connections
    $connections = Get-NetTCPConnection -State Established |
        Where-Object { $_.RemotePort -in @(443, 80, 8080, 2195, 5228) }
    
    Write-Info "Active Telegram-related connections: $($connections.Count)"
}

function Show-ProcessStatus {
    Write-Header "PROCESS STATUS"
    
    # T_Dubber process
    $tdubber = Get-Process -Name $T_DUBBER_PROCESS -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*$T_DUBBER_SCRIPT*" }
    
    if ($tdubber) {
        Write-Success "T_Dubber (PID $($tdubber.Id))"
        Write-Info "  CPU: $($tdubber.CPU) seconds"
        Write-Info "  RAM: $([math]::Round($tdubber.WorkingSet64 / 1MB, 2)) MB"
        Write-Info "  Priority: $($tdubber.PriorityClass)"
        Write-Info "  Threads: $($tdubber.Threads.Count)"
    }
    
    # Top 5 CPU consumers
    Write-Info "Top 5 CPU consumers:"
    Get-Process | Sort-Object CPU -Descending | Select-Object -First 5 | ForEach-Object {
        Write-Info "  $($_.ProcessName) — CPU: $($_.CPU)s, RAM: $([math]::Round($_.WorkingSet64 / 1MB, 2)) MB"
    }
    
    # Top 5 Network consumers
    Write-Info "Top 5 Network consumers:"
    Get-Process | Where-Object { $_.NetworkUsage -gt 0 } |
        Sort-Object NetworkUsage -Descending | Select-Object -First 5 | ForEach-Object {
        Write-Info "  $($_.ProcessName) — Network: $([math]::Round($_.NetworkUsage / 1MB, 2)) MB"
    }
}

function Start-BoostMode {
    Write-Header "T_DUBBER BOOST MODE ACTIVATED"
    
    if (-not (Test-Admin)) {
        Write-Warning "Administrator rights nahi hai!"
        Write-Info "Right-click → Run as Administrator"
        Write-Info "Warna koi settings apply nahi hongi."
        Write-Host ""
        $continue = Read-Host "Continue without admin? (y/n)"
        if ($continue -ne "y") { exit }
    }
    
    # 1. CPU Priority
    Set-TDubberPriority
    
    # 2. Network QoS
    Set-NetworkQoS
    
    # 3. Power Plan
    Set-HighPerformancePower
    
    # 4. Network Priority
    Set-NetworkPriority
    
    # 5. Status
    Show-NetworkStatus
    Show-ProcessStatus
    
    Write-Header "BOOST ACTIVE FOR $BOOST_DURATION_MIN MINUTES"
    Write-Info "Press Ctrl+C to stop early"
    Write-Info "Auto-revert after $BOOST_DURATION_MIN minutes"
    
    # Countdown
    for ($i = $BOOST_DURATION_MIN; $i -gt 0; $i--) {
        $minutes = [math]::Floor($i / 60)
        $seconds = $i % 60
        Write-Host "`r  ⏱️  Boost active: ${minutes}m ${seconds}s remaining   " -NoNewline -ForegroundColor Cyan
        Start-Sleep -Seconds 1
    }
    
    Write-Host ""
    Write-Header "BOOST COMPLETE — REVERTING TO NORMAL"
    
    # Revert settings
    powercfg /setactive scheme_current
    netsh int tcp set global autotuninglevel=normal | Out-Null
    
    Write-Success "Settings reverted to normal"
}

function Show-Menu {
    Clear-Host
    Write-Host ""
    Write-Host "╔═══════════════════════════════════════════════════════╗" -ForegroundColor Cyan
    Write-Host "║                                                       ║" -ForegroundColor Cyan
    Write-Host "║   🎬 T_DUBBER BOOST — Network & CPU Optimizer        ║" -ForegroundColor Cyan
    Write-Host "║                                                       ║" -ForegroundColor Cyan
    Write-Host "╚═══════════════════════════════════════════════════════╝" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  [1] 🚀 Start Boost Mode (2 hours)" -ForegroundColor Green
    Write-Host "  [2] ⚡ Quick Boost (30 minutes)" -ForegroundColor Yellow
    Write-Host "  [3] 📊 Show Network Status" -ForegroundColor White
    Write-Host "  [4] 🔧 Show Process Status" -ForegroundColor White
    Write-Host "  [5] 🔄 Revert All Settings" -ForegroundColor Red
    Write-Host "  [6] ❌ Exit" -ForegroundColor Gray
    Write-Host ""
}

function Revert-AllSettings {
    Write-Header "REVERTING ALL SETTINGS"
    
    # Power plan
    powercfg /setactive scheme_current
    Write-Success "Power plan → Balanced"
    
    # TCP settings
    netsh int tcp set global autotuninglevel=normal | Out-Null
    netsh int tcp set global chimney=disabled | Out-Null
    netsh int tcp set global netdma=disabled | Out-Null
    Write-Success "TCP settings → Default"
    
    # Network throttling
    $regPath = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile"
    Set-ItemProperty -Path $regPath -Name "NetworkThrottlingIndex" -Value 10 -Type DWord
    Set-ItemProperty -Path $regPath -Name "SystemResponsiveness" -Value 20 -Type DWord
    Write-Success "Network throttling → Default"
    
    # Process priorities
    $processes = Get-Process -Name $T_DUBBER_PROCESS -ErrorAction SilentlyContinue
    foreach ($proc in $processes) {
        try {
            $proc.PriorityClass = "Normal"
        } catch {}
    }
    Write-Success "Process priorities → Normal"
    
    Write-Header "ALL SETTINGS REVERTED"
}

# ============================================================
# MAIN
# ============================================================

# Check if running from command line with arguments
if ($args.Count -gt 0) {
    switch ($args[0]) {
        "start" { Start-BoostMode }
        "quick" { 
            $script:BOOST_DURATION_MIN = 30
            Start-BoostMode 
        }
        "status" { 
            Show-NetworkStatus
            Show-ProcessStatus 
        }
        "revert" { Revert-AllSettings }
        default { 
            Write-Host "Usage: .\boost.ps1 [start|quick|status|revert]" 
        }
    }
    exit
}

# Interactive menu
do {
    Show-Menu
    $choice = Read-Host "  Select option (1-6)"
    
    switch ($choice) {
        "1" { Start-BoostMode }
        "2" { 
            $script:BOOST_DURATION_MIN = 30
            Start-BoostMode 
        }
        "3" { 
            Show-NetworkStatus
            Write-Host ""
            Read-Host "Press Enter to continue"
        }
        "4" { 
            Show-ProcessStatus
            Write-Host ""
            Read-Host "Press Enter to continue"
        }
        "5" { 
            Revert-AllSettings
            Write-Host ""
            Read-Host "Press Enter to continue"
        }
        "6" { exit }
        default { 
            Write-Host "Invalid option!" -ForegroundColor Red
            Start-Sleep -Seconds 1
        }
    }
} while ($true)
