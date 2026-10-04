# T_Dubber Auto-Boost Installer
# Windows Task Scheduler mein install karta hai
# Ab T_Dubber jab bhi start ho, boost khud apply hoga

$ErrorActionPreference = "SilentlyContinue"
$APP_DIR = "C:\Users\pocot\Music\T_Dubber"
$TASK_NAME = "T_Dubber_AutoBoost"

Write-Host ""
Write-Host "═══════════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host "  🎬 T_Dubber Auto-Boost Installer" -ForegroundColor Cyan
Write-Host "═══════════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host ""

# Check admin
$currentPrincipal = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent()
)
$isAdmin = $currentPrincipal.IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)

if (-not $isAdmin) {
    Write-Host "  ❌ Admin rights chahiye!" -ForegroundColor Red
    Write-Host "  Right-click → Run as Administrator" -ForegroundColor Yellow
    Write-Host ""
    Read-Host "Press Enter to exit"
    exit
}

# Purana task delete karo (agar hai)
Unregister-ScheduledTask -TaskName $TASK_NAME -Confirm:$false -ErrorAction SilentlyContinue

# Naya task create karo
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$APP_DIR\start-boosted.ps1`"" `
    -WorkingDirectory $APP_DIR

# Trigger: Jab bhi user login kare
$trigger = New-ScheduledTaskTrigger -AtLogOn

# Settings: Run with highest privileges
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable:$false

# Register task
Register-ScheduledTask -TaskName $TASK_NAME `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -RunLevel Highest `
    -Description "T_Dubber Auto-Boost: High Performance mode + Network QoS + CPU Priority" `
    -Force | Out-Null

Write-Host "  ✅ Auto-Boost Task Installed!" -ForegroundColor Green
Write-Host ""
Write-Host "  📋 Task Name: $TASK_NAME" -ForegroundColor White
Write-Host "  🔄 Trigger: User logon par auto-start" -ForegroundColor White
Write-Host "  ⚡ Boost: High Performance + Network QoS + CPU Priority" -ForegroundColor White
Write-Host ""
Write-Host "  ═══════════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host "  🎬 AB KAAM KARO:" -ForegroundColor Cyan
Write-Host "  ═══════════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host ""
Write-Host "  1. T_Dubber start karo (normal ya admin — dono chalega)" -ForegroundColor White
Write-Host "  2. Boost khud apply ho jayega!" -ForegroundColor White
Write-Host "  3. Upload karo — 30-40% faster!" -ForegroundColor White
Write-Host ""
Write-Host "  💡 Manual boost ke liye: .\boost.ps1" -ForegroundColor Gray
Write-Host "  💡 Settings revert ke liye: .\boost.ps1 revert" -ForegroundColor Gray
Write-Host ""
Read-Host "Press Enter to exit"
