# T_Dubber Auto-Boost Installer
# Windows Task Scheduler mein install karta hai
# Ab T_Dubber jab bhi start ho, boost khud apply hoga
#
# ############################################################################
#  ⚠️  WARNING — READ BEFORE RUNNING THIS SCRIPT
#
#  It creates an ELEVATED (-RunLevel Highest) scheduled task named
#  'T_Dubber_AutoBoost' that fires at EVERY user logon and runs
#  start-boosted.ps1 as Administrator — which (historically) applied
#  SYSTEM-WIDE network/power settings: HKLM registry values, netsh TCP
#  globals, a firewall rule, the power plan + USB policy, and it launched
#  app.py itself ELEVATED (a Python program running with full admin rights).
#
#    * DEFAULT = DRY RUN. Running this file with no arguments only SHOWS the
#      task that would be created. Nothing is installed, no UAC, no admin.
#    * Pass -Force (from an elevated PowerShell) to actually create it.
#    * start-boosted.ps1 has been rewritten to make ZERO system-wide changes,
#      so today this task's only real effect is auto-starting app.py elevated
#      at logon. Think twice before using -Force.
#    * UNINSTALL at any time:
#        Unregister-ScheduledTask -TaskName T_Dubber_AutoBoost -Confirm:$false
# ############################################################################

[CmdletBinding()]
param(
    # Default = dry-run (print the plan only). Pass -Force to really create
    # the elevated at-logon task (also requires Administrator).
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$APP_DIR = "C:\Users\pocot\Music\T_Dubber"
$TASK_NAME = "T_Dubber_AutoBoost"
$UNINSTALL_CMD = 'Unregister-ScheduledTask -TaskName T_Dubber_AutoBoost -Confirm:$false'

Write-Host ""
Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "  T_Dubber Auto-Boost Installer" -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host ""

# Check admin (sirf -Force path ise zarurat hai; dry-run kahin bhi chalega)
$currentPrincipal = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent()
)
$isAdmin = $currentPrincipal.IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)

# ============================================================
# Task definition (banane se PEHLE dry-run me yahi dikhata hai)
# ============================================================
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

Write-Host "  Task that WOULD be created:" -ForegroundColor White
Write-Host "    Name      : $TASK_NAME" -ForegroundColor Gray
Write-Host "    Action    : powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$APP_DIR\start-boosted.ps1`"" -ForegroundColor Gray
Write-Host "    Work dir  : $APP_DIR" -ForegroundColor Gray
Write-Host "    Trigger   : AtLogOn (har user logon par)" -ForegroundColor Gray
Write-Host "    Run level : Highest  <-- ELEVATED (admin) at every logon" -ForegroundColor Yellow
Write-Host "    Uninstall : $UNINSTALL_CMD" -ForegroundColor Gray
Write-Host ""

if (-not $Force) {
    # ---------------- DRY RUN (default) ----------------
    Write-Host "  [!]  DRY RUN — nothing was created (no -Force given)." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  This task, if installed, launches start-boosted.ps1 (and therefore" -ForegroundColor Gray
    Write-Host "  app.py) ELEVATED at every logon — it does not need admin anymore." -ForegroundColor Gray
    Write-Host ""
    Write-Host "  To actually install it:   .\install-autoboost.ps1 -Force" -ForegroundColor White
    Write-Host "  (run from an elevated PowerShell)" -ForegroundColor Gray
    Write-Host "  To uninstall later:        $UNINSTALL_CMD" -ForegroundColor White
    Write-Host ""
    if (-not [Console]::IsInputRedirected) { Read-Host "Press Enter to exit" }
    exit 0
}

# ============================================================
# -Force path: warn again, require admin, then create
# ============================================================

Write-Host "  ⚠️  ABOUT TO CREATE AN ELEVATED AT-LOGON SCHEDULED TASK" -ForegroundColor Yellow
Write-Host "      '$TASK_NAME' runs PowerShell (and start-boosted.ps1 / app.py)" -ForegroundColor Yellow
Write-Host "      with HIGHEST privileges every time any user logs on, and it can" -ForegroundColor Yellow
Write-Host "      change system-wide network/power settings." -ForegroundColor Yellow
Write-Host ""

if (-not $isAdmin) {
    Write-Host "  ❌ Admin rights chahiye!" -ForegroundColor Red
    Write-Host "  Right-click → Run as Administrator" -ForegroundColor Yellow
    Write-Host ""
    if (-not [Console]::IsInputRedirected) { Read-Host "Press Enter to exit" }
    exit 1
}

# Purana task delete karo (agar hai)
Unregister-ScheduledTask -TaskName $TASK_NAME -Confirm:$false -ErrorAction SilentlyContinue

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
Write-Host "  ⚡ Boost: elevated start-boosted.ps1 at every logon" -ForegroundColor White
Write-Host ""
Write-Host "  🗑️  UNINSTALL anytime:" -ForegroundColor Yellow
Write-Host "     $UNINSTALL_CMD" -ForegroundColor White
Write-Host ""
Write-Host "  💡 Manual boost ke liye: .\boost.ps1  (system-wide, admin)" -ForegroundColor Gray
Write-Host "  💡 Settings revert ke liye: .\revert-boost.ps1  (dry-run by default)" -ForegroundColor Gray
Write-Host ""
if (-not [Console]::IsInputRedirected) { Read-Host "Press Enter to exit" }
