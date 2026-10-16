# T_Dubber System-Settings Revert - non-admin first, DRY-RUN by default
# Windows defaults wapas laata hai jo boost.ps1 / purane start-boosted.ps1 /
# install-autoboost.ps1 ne machine-wide change kiye the.
#
# USAGE
#   .\revert-boost.ps1                    # DRY-RUN: current state + plan, kuch
#                                         # change nahi hota (default, non-admin chalega)
#   .\revert-boost.ps1 -Execute           # actually restore (elevated PowerShell chahiye)
#   .\revert-boost.ps1 -Elevate -Execute  # ek UAC prompt le kar khud elevate ho jata hai
#
# WHAT IT RESTORES (all machine-wide, all idempotent):
#   1. HKLM NetworkThrottlingIndex  -> 10   (Windows default)
#   2. HKLM SystemResponsiveness    -> 20   (Windows default)
#   3. Firewall rule 'T_Dubber_Priority' -> delete
#   4. netsh TCP autotuninglevel=normal, rss=enabled
#   5. 12 demoted apps (chrome/edge/steam/discord/...) -> Normal priority
#   6. re-activate scheme_current power plan (+ switch away from boost's
#      duplicate 'High performance' scheme if it is active)
#   7. USB selective suspend (AC) -> 1 (Windows default), then re-activate
#   8. delete duplicate scheme 8c5e7fda-5397-4f7c-9a4f-1499e67a3981 if present
#
# NOTES
#   * Dry-run never asks for UAC. UAC happens ONLY with -Elevate (never forced).
#   * Without admin, only the two HKLM values (plus firewall/power/netsh) need
#     elevation - the script prints the exact elevated commands for those two.
#   * Idempotent: re-running reports "OK" for everything already restored.
#   * auto.py (Python) applies the same HKLM/netsh tweaks with no revert path
#     and is out of scope here - do not run it after restoring.

[CmdletBinding()]
param(
    # Default = dry-run (show what would change). Pass -Execute to apply.
    [switch]$Execute,
    # Ask for elevation via one UAC prompt. Never forced without this switch.
    [switch]$Elevate
)

$ErrorActionPreference = 'Stop'

# ============================================================
# CONFIGURATION
# ============================================================
$HKLM_PROFILE = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile'
$FIREWALL_RULE = 'T_Dubber_Priority'
$DUP_SCHEME = '8c5e7fda-5397-4f7c-9a4f-1499e67a3981'      # boost-created copy
$BALANCED_SCHEME = '381b4222-f694-41f0-9685-ff5bb260df2e'  # Windows default plan
$USB_SUBGROUP = '2a737441-1930-4402-8d77-b2bebba308a3'     # USB settings
$USB_SETTING = '48e6b7a6-50f5-4782-a5d4-53bb8f07e226'      # selective suspend (AC)
$DEMOTE_APPS = @(
    'chrome', 'firefox', 'edge', 'steam', 'discord', 'teams',
    'zoom', 'onedrive', 'dropbox', 'torrent', 'qbittorrent', 'epicgameslauncher'
)

# ============================================================
# OUTPUT HELPERS (same style as boost.ps1)
# ============================================================

function Write-Header($text) {
    Write-Host ""
    Write-Host "==========================================================" -ForegroundColor Cyan
    Write-Host "  $text" -ForegroundColor Cyan
    Write-Host "==========================================================" -ForegroundColor Cyan
}

function Write-Success($text) { Write-Host "  [ok] $text" -ForegroundColor Green }
function Write-Warning($text) { Write-Host "  [!]  $text" -ForegroundColor Yellow }
function Write-Info($text)    { Write-Host "  [i]  $text" -ForegroundColor Gray }
function Write-Fail($text)    { Write-Host "  [X]  $text" -ForegroundColor Red }

function Test-Admin {
    $principal = New-Object Security.Principal.WindowsPrincipal(
        [Security.Principal.WindowsIdentity]::GetCurrent()
    )
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

# Native stderr must never become a terminating error under EAP=Stop.
function Invoke-Netsh {
    param([string[]]$NetshArgs)
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'SilentlyContinue'
    try {
        $out = (& netsh @NetshArgs 2>&1 | ForEach-Object { "$_" }) -join "`n"
        return [pscustomobject]@{ Output = $out; ExitCode = $LASTEXITCODE }
    } finally {
        $ErrorActionPreference = $old
    }
}

function Get-DwordValue {
    param([string]$Path, [string]$Name)
    $item = Get-ItemProperty -Path $Path -Name $Name -ErrorAction SilentlyContinue
    if ($null -eq $item) { return $null }
    $value = $item.$Name
    if ($null -eq $value) { return $null }
    # DWord 0xffffffff reads back as -1; normalise to unsigned for comparing.
    return ([int64]$value) -band 0xFFFFFFFFL
}

function Test-FirewallRule {
    param([string]$Name)
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'SilentlyContinue'
    try {
        return [bool](Get-NetFirewallRule -Name $Name -ErrorAction SilentlyContinue)
    } finally {
        $ErrorActionPreference = $old
    }
}

function Get-TcpGlobalValue {
    param([string]$Pattern)
    $r = Invoke-Netsh -NetshArgs @('int', 'tcp', 'show', 'global')
    if ($r.Output -match $Pattern) { return $Matches[1].Trim() }
    return $null
}

function Get-PowerCfgAcValue {
    param([string]$Subgroup, [string]$Setting)
    try {
        $out = (& powercfg /query scheme_current $Subgroup $Setting | Out-String)
    } catch {
        return $null
    }
    if ($out -match 'Current AC Power Setting Index:\s*0x([0-9a-fA-F]+)') {
        return [Convert]::ToInt32($Matches[1], 16)
    }
    return $null
}

function Show-HKLMRestoreHint {
    Write-Warning "Not running as Administrator."
    Write-Host ""
    Write-Host "  Exactly these TWO HKLM values need a one-time elevated restore:" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "    HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile" -ForegroundColor White
    Write-Host "        NetworkThrottlingIndex = 10   (Windows default; boost set 4294967295)" -ForegroundColor Gray
    Write-Host "        SystemResponsiveness   = 20   (Windows default; boost set 0)" -ForegroundColor Gray
    Write-Host ""
    Write-Host "  Run ONCE in an ELEVATED PowerShell:" -ForegroundColor Yellow
    Write-Host "    Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile' -Name NetworkThrottlingIndex -Value 10 -Type DWord" -ForegroundColor White
    Write-Host "    Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile' -Name SystemResponsiveness -Value 20 -Type DWord" -ForegroundColor White
    Write-Host ""
    Write-Host "  ...or let this script restore EVERYTHING (needs one UAC prompt):" -ForegroundColor Yellow
    Write-Host "    .\revert-boost.ps1 -Elevate -Execute" -ForegroundColor White
    Write-Host ""
}

# ============================================================
# ELEVATION (explicit -Elevate only, never forced)
# ============================================================

$isAdmin = Test-Admin

if (-not $isAdmin) {
    Show-HKLMRestoreHint
    if ($Elevate) {
        $argLine = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
        if ($Execute) { $argLine += ' -Execute' }
        $argLine += ' -Elevate'
        Write-Info "Requesting elevation (UAC prompt)..."
        try {
            $elevated = Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $argLine -PassThru -Wait
        } catch {
            Write-Fail "Elevation cancelled or failed: $($_.Exception.Message)"
            exit 1
        }
        exit $elevated.ExitCode
    }
}

# ============================================================
# DETECTION (read-only - safe without admin, this is the dry-run plan)
# ============================================================

$actions = New-Object System.Collections.ArrayList
$results = New-Object System.Collections.ArrayList

function Register-Action {
    param(
        [string]$Name,
        [bool]$Needed,
        [string]$Detail,
        [scriptblock]$Apply
    )
    [void]$actions.Add([pscustomobject]@{
            Name   = $Name
            Needed = $Needed
            Detail = $Detail
            Apply  = $Apply
        })
}

function Add-Result {
    param([string]$Name, [string]$Status, [string]$Detail)
    [void]$results.Add([pscustomobject]@{ Name = $Name; Status = $Status; Detail = $Detail })
}

# --- 1. HKLM NetworkThrottlingIndex (default 10) --------------------------
$nti = Get-DwordValue -Path $HKLM_PROFILE -Name NetworkThrottlingIndex
$ntiNeeded = ($null -ne $nti) -and ($nti -ne 10)
if ($null -eq $nti) { $ntiDetail = 'not set (Windows default applies)' }
elseif ($ntiNeeded) { $ntiDetail = "$nti -> restore 10 (Windows default)" }
else { $ntiDetail = "$nti (= Windows default)" }
Register-Action -Name 'HKLM NetworkThrottlingIndex' -Needed $ntiNeeded -Detail $ntiDetail `
    -Apply { Set-ItemProperty -Path $HKLM_PROFILE -Name NetworkThrottlingIndex -Value 10 -Type DWord }

# --- 2. HKLM SystemResponsiveness (default 20) ----------------------------
$sr = Get-DwordValue -Path $HKLM_PROFILE -Name SystemResponsiveness
$srNeeded = ($null -ne $sr) -and ($sr -ne 20)
if ($null -eq $sr) { $srDetail = 'not set (Windows default applies)' }
elseif ($srNeeded) { $srDetail = "$sr -> restore 20 (Windows default)" }
else { $srDetail = "$sr (= Windows default)" }
Register-Action -Name 'HKLM SystemResponsiveness' -Needed $srNeeded -Detail $srDetail `
    -Apply { Set-ItemProperty -Path $HKLM_PROFILE -Name SystemResponsiveness -Value 20 -Type DWord }

# --- 3. Firewall rule created by boost.ps1 --------------------------------
$fwPresent = Test-FirewallRule -Name $FIREWALL_RULE
$fwDetail = if ($fwPresent) { "exists -> netsh delete rule name=$FIREWALL_RULE" } else { 'absent (Windows default)' }
Register-Action -Name "Firewall rule '$FIREWALL_RULE'" -Needed $fwPresent -Detail $fwDetail `
    -Apply {
        $r = Invoke-Netsh -NetshArgs @('advfirewall', 'firewall', 'delete', 'rule', "name=$FIREWALL_RULE")
        if (Test-FirewallRule -Name $FIREWALL_RULE) { throw "netsh could not delete the rule: $($r.Output.Trim())" }
    }

# --- 4/5. netsh TCP globals ------------------------------------------------
$autotune = Get-TcpGlobalValue -Pattern 'Receive Window Auto-Tuning Level\s*:\s*(\S+)'
$autotuneNeeded = ($autotune -ne 'normal')   # unreadable -> set it anyway (idempotent)
$autotuneDetail = if ($null -eq $autotune) { 'could not read -> set normal' }
elseif ($autotuneNeeded) { "$autotune -> restore normal" } else { "$autotune (= Windows default)" }
Register-Action -Name 'netsh TCP autotuninglevel' -Needed $autotuneNeeded -Detail $autotuneDetail `
    -Apply {
        $r = Invoke-Netsh -NetshArgs @('int', 'tcp', 'set', 'global', 'autotuninglevel=normal')
        $now = Get-TcpGlobalValue -Pattern 'Receive Window Auto-Tuning Level\s*:\s*(\S+)'
        if ($now -ne 'normal') { throw "netsh did not apply autotuninglevel=normal: $($r.Output.Trim())" }
    }

$rss = Get-TcpGlobalValue -Pattern 'Receive-Side Scaling State\s*:\s*(\S+)'
$rssNeeded = ($rss -ne 'enabled')
$rssDetail = if ($null -eq $rss) { 'could not read -> set enabled' }
elseif ($rssNeeded) { "$rss -> restore enabled" } else { "$rss (= Windows default)" }
Register-Action -Name 'netsh TCP rss' -Needed $rssNeeded -Detail $rssDetail `
    -Apply {
        $r = Invoke-Netsh -NetshArgs @('int', 'tcp', 'set', 'global', 'rss=enabled')
        $now = Get-TcpGlobalValue -Pattern 'Receive-Side Scaling State\s*:\s*(\S+)'
        if ($now -ne 'enabled') { throw "netsh did not apply rss=enabled: $($r.Output.Trim())" }
    }

# --- 6. Apps boost demoted to BelowNormal -> back to Normal ---------------
$demoted = @()
foreach ($procName in $DEMOTE_APPS) {
    $found = Get-Process -Name $procName -ErrorAction SilentlyContinue | Where-Object {
        try { "$($_.PriorityClass)" -eq 'BelowNormal' } catch { $false }
    }
    if ($found) { $demoted += @($found) }
}
$demotedNeeded = ($demoted.Count -gt 0)
$demotedDetail = if ($demotedNeeded) {
    (($demoted | ForEach-Object { "$($_.ProcessName) (PID $($_.Id))" }) -join ', ') + ' -> Normal'
} else {
    'no throttled app running (all at Normal or not started)'
}
Register-Action -Name 'Demoted app priorities (12-app list)' -Needed $demotedNeeded -Detail $demotedDetail `
    -Apply {
        $restored = @()
        $failed = @()
        foreach ($p in $demoted) {
            try {
                $p.Refresh()
                if ("$($p.PriorityClass)" -eq 'BelowNormal') { $p.PriorityClass = 'Normal' }
                $restored += "$($p.ProcessName) (PID $($p.Id))"
            } catch {
                $failed += "$($p.ProcessName) (PID $($p.Id)): $($_.Exception.Message)"
            }
        }
        if ($failed.Count -gt 0) { throw "could not restore: $($failed -join '; ')" }
    }

# --- 7. Active power plan (must leave boost's duplicate before deleting it)
$activeGuid = $null
$activeName = ''
$activeLine = "$(powercfg /getactivescheme)"
if ($activeLine -match 'Power Scheme GUID:\s*([0-9a-fA-F-]+)\s*(?:\((.*?)\))?') {
    $activeGuid = $Matches[1]
    if ($Matches[2]) { $activeName = $Matches[2] }
}
$activeIsDup = ($activeGuid -and ($activeGuid -ieq $DUP_SCHEME))
$activeDetail = if ($activeIsDup) {
    "'$activeName' is boost's duplicate -> switch to Balanced (Windows default)"
} else {
    "'$activeName' (scheme_current, left as is)"
}
Register-Action -Name 'Active power plan' -Needed $activeIsDup -Detail $activeDetail `
    -Apply { powercfg /setactive $BALANCED_SCHEME | Out-Null }

# --- 8. USB selective suspend (AC) -> Windows default 1 -------------------
$usbAc = Get-PowerCfgAcValue -Subgroup $USB_SUBGROUP -Setting $USB_SETTING
$usbNeeded = ($usbAc -ne 1)
$usbDetail = if ($null -eq $usbAc) { 'could not read -> set 1 (Windows default)' }
elseif ($usbNeeded) { "$usbAc -> restore 1 (Windows default)" } else { "$usbAc (= Windows default)" }
Register-Action -Name 'USB selective suspend (AC)' -Needed $usbNeeded -Detail $usbDetail `
    -Apply {
        powercfg /setacvalueindex scheme_current $USB_SUBGROUP $USB_SETTING 1 | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "powercfg /setacvalueindex failed (exit $LASTEXITCODE)" }
        powercfg /setactive scheme_current | Out-Null   # re-activate to flush
    }

# --- 9. Duplicate 'High performance' scheme created by boost --------------
$schemeList = @(powercfg /list)
$dupPresent = [bool]($schemeList | Where-Object { $_ -match [regex]::Escape($DUP_SCHEME) })
$dupDetail = if ($dupPresent) { 'present in powercfg /list -> delete' } else { 'not present' }
Register-Action -Name "Duplicate scheme $DUP_SCHEME" -Needed $dupPresent -Detail $dupDetail `
    -Apply {
        powercfg /delete $DUP_SCHEME | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "powercfg /delete failed (exit $LASTEXITCODE)" }
        if (powercfg /list | Select-String -SimpleMatch $DUP_SCHEME) { throw 'scheme still listed after delete' }
    }

# ============================================================
# APPLY (only with -Execute, only when elevated) OR REPORT
# ============================================================

if ($Execute -and -not $isAdmin) {
    Write-Header 'ELEVATION REQUIRED'
    Write-Warning '-Execute needs an elevated PowerShell for HKLM / firewall / netsh / powercfg.'
    Write-Info 'Re-run as:  .\revert-boost.ps1 -Elevate -Execute'
    Write-Info '(the two HKLM values printed above are the mandatory one-time restore)'
    exit 1
}

foreach ($a in $actions) {
    if (-not $a.Needed) {
        Add-Result -Name $a.Name -Status 'OK' -Detail $a.Detail
        continue
    }
    if (-not $Execute) {
        Add-Result -Name $a.Name -Status 'WOULD CHANGE' -Detail $a.Detail
        continue
    }
    try {
        & $a.Apply
        Add-Result -Name $a.Name -Status 'CHANGED' -Detail $a.Detail
    } catch {
        Add-Result -Name $a.Name -Status 'FAILED' -Detail "$($_.Exception.Message)"
    }
}

if ($Execute) {
    # Re-activate scheme_current: flushes the pending power-setting changes.
    powercfg /setactive scheme_current | Out-Null
    Write-Info "powercfg /setactive scheme_current  (re-activated / flushed)"
}

# ============================================================
# SUMMARY
# ============================================================

Write-Header 'SUMMARY'
$counts = @{ OK = 0; 'WOULD CHANGE' = 0; CHANGED = 0; FAILED = 0 }
foreach ($r in $results) {
    $counts[$r.Status] = $counts[$r.Status] + 1
    switch ($r.Status) {
        'OK' { Write-Success "$($r.Name) - $($r.Detail)" }
        'WOULD CHANGE' { Write-Warning "$($r.Name) - $($r.Detail)" }
        'CHANGED' { Write-Success "$($r.Name) - $($r.Detail)  [CHANGED]" }
        'FAILED' { Write-Fail "$($r.Name) - $($r.Detail)  [FAILED]" }
    }
}
Write-Host ""

if (-not $Execute) {
    Write-Warning "DRY RUN - nothing was changed. ($($counts['WOULD CHANGE']) item(s) would change, $($counts['OK']) already OK)"
    if ($isAdmin) { Write-Info 'Apply with:  .\revert-boost.ps1 -Execute' }
    else { Write-Info 'Apply with:  .\revert-boost.ps1 -Elevate -Execute   (one UAC prompt, no admin right now)' }
} else {
    Write-Info "$($counts['CHANGED']) changed, $($counts['OK']) already OK, $($counts['FAILED']) failed"
}

if ($counts['FAILED'] -gt 0) { exit 1 }
exit 0
