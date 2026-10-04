# T_Dubber Auto-Boost Launcher
# Jab bhi app.py start ho, khud boost apply ho jaye
# Bas ye chalao — baaki sab automatic
#
# SPEED POLICY (AI/dev note): ye boost.ps1 ka silent version hai — same CPU /
# power / TCP tuning, bina menu ke, phir app.py High priority me start. Python
# (Telethon) ya Go (parallel x4) jo bhi upload kare, dono ko headroom milta
# hai. Correctness pe zero effect; sirf machine fast.

$ErrorActionPreference = "SilentlyContinue"
$APP_DIR = "C:\Users\pocot\Music\T_Dubber"

# ============================================================
# AUTO-BOOST SETTINGS (Background mein apply)
# ============================================================

function Start-SilentBoost {
    # 1. Power Plan → High Performance
    $highPerf = powercfg /list | Select-String "High performance"
    if ($highPerf) {
        $guid = ($highPerf -split "\s+")[3]
        powercfg /setactive $guid | Out-Null
    }

    # 2. Network Throttling Disable
    $regPath = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile"
    Set-ItemProperty -Path $regPath -Name "NetworkThrottlingIndex" -Value 0xffffffff -Type DWord -ErrorAction SilentlyContinue
    Set-ItemProperty -Path $regPath -Name "SystemResponsiveness" -Value 0 -Type DWord -ErrorAction SilentlyContinue

    # 3. TCP Optimization
    netsh int tcp set global autotuninglevel=normal | Out-Null
    netsh int tcp set global rss=enabled | Out-Null
    netsh int tcp set global chimney=enabled | Out-Null
    netsh int tcp set global netdma=enabled | Out-Null
    netsh int tcp set global dca=enabled | Out-Null

    # 4. Other apps ko BelowNormal priority
    $throttleProcs = @("chrome", "firefox", "edge", "steam", "epicgameslauncher", "torrent", "qbittorrent", "onedrive", "dropbox", "teams", "zoom", "discord")
    foreach ($procName in $throttleProcs) {
        $procs = Get-Process -Name $procName -ErrorAction SilentlyContinue
        if ($procs) {
            foreach ($proc in $procs) {
                try { $proc.PriorityClass = "BelowNormal" } catch {}
            }
        }
    }
}

# ============================================================
# T_DUBBER KO HIGH PRIORITY MEIN START
# ============================================================

function Start-TDubberWithBoost {
    Set-Location $APP_DIR
    
    # Pehle silent boost apply karo
    Start-SilentBoost
    
    # T_Dubber ko High Priority se start karo
    $pythonPath = (Get-Command python).Source
    $proc = Start-Process -FilePath $pythonPath -ArgumentList "app.py" -WorkingDirectory $APP_DIR -PassThru -WindowStyle Normal
    
    # Priority set karo
    Start-Sleep -Seconds 3
    try {
        $proc.PriorityClass = "High"
        $proc.ProcessorAffinity = 0xFFFF
    } catch {}
    
    Write-Host ""
    Write-Host "═══════════════════════════════════════════════════════" -ForegroundColor Green
    Write-Host "  🎬 T_Dubber BOOSTED MODE mein start ho gaya!" -ForegroundColor Green
    Write-Host "═══════════════════════════════════════════════════════" -ForegroundColor Green
    Write-Host ""
    Write-Host "  ✅ Power Plan: High Performance" -ForegroundColor Green
    Write-Host "  ✅ Network Throttling: Disabled" -ForegroundColor Green
    Write-Host "  ✅ TCP Optimization: Enabled" -ForegroundColor Green
    Write-Host "  ✅ Other Apps: Throttled" -ForegroundColor Green
    Write-Host "  ✅ T_Dubber Priority: High" -ForegroundColor Green
    Write-Host ""
    Write-Host "  📊 Process ID: $($proc.Id)" -ForegroundColor Cyan
    Write-Host "  🌐 Gradio UI: http://127.0.0.1:7860" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  💡 Band karne ke liye: Task Manager → python → Priority Normal" -ForegroundColor Gray
    Write-Host ""
    
    # Process monitor (optional)
    $proc.WaitForExit()
}

# ============================================================
# MAIN
# ============================================================

# Check admin
$currentPrincipal = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent()
)
$isAdmin = $currentPrincipal.IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)

if (-not $isAdmin) {
    Write-Host ""
    Write-Host "═══════════════════════════════════════════════════════" -ForegroundColor Yellow
    Write-Host "  ⚠️  ADMIN RIGHTS NAHI HAI" -ForegroundColor Yellow
    Write-Host "═══════════════════════════════════════════════════════" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  Full boost ke liye Admin mode mein chalao:" -ForegroundColor Yellow
    Write-Host "  Right-click → Run as Administrator" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  Ab bina admin ke start ho raha hai (partial boost)..." -ForegroundColor Gray
    Write-Host ""
    Start-Sleep -Seconds 2
}

# Start T_Dubber with boost
Start-TDubberWithBoost
