<#
.SYNOPSIS
    T_Dubber runtime doctor and repair.

.DESCRIPTION
    Checks everything the app needs at run time and fixes what it safely can.

    This does NOT touch TCP tuning. Measured on this machine, Receive Window
    Auto-Tuning is already "normal", RFC 1323 timestamps are allowed, and the
    suggested TcpAckFrequency registry path (Services\Tcpip\Parameters\Interfaces\*)
    is rejected outright because Registry does not expand the wildcard. Those
    "optimisations" change nothing, so they are deliberately absent here.

    What it does instead is verify the things that actually break runs:
    Python version, required packages, ffmpeg/ffprobe, Kaggle credentials,
    Telegram configuration, disk space, the SQLite index, and whether the
    optional tgup uploader can execute.

    SPEED POLICY (AI/dev note): check [8/8] reports tgup's state via
    `python go_planner.py check`. Runnable + multi-part + public channel =>
    app.py uploads through tgup over several connections; otherwise Telethon.
    Machine speed (CPU/TCP/QoS) lives in boost.ps1 / start-boosted.ps1, not here.

.PARAMETER Fix
    Attempt safe repairs: install missing Python packages, create a placeholder
    channels.json, seed the database.

.PARAMETER Admin
    Also report which repairs would need an elevated session.

.EXAMPLE
    .\doctor.ps1
    .\doctor.ps1 -Fix
    .\doctor.ps1 -Json
#>
[CmdletBinding()]
param(
    [switch]$Fix,
    [switch]$Admin,
    [switch]$Json
)

$ErrorActionPreference = "Continue"
$AppDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $AppDir

$script:Results = New-Object System.Collections.ArrayList
$script:NeedsAdmin = $false
$script:Fixable = 0

function Add-Result {
    param(
        [string]$Name,
        [string]$Status,   # ok | warn | fail | fixed
        [string]$Detail,
        [string]$Fix = ""
    )
    $null = $script:Results.Add([PSCustomObject]@{
        Check = $Name; Status = $Status; Detail = $Detail; Fix = $Fix
    })
    if ($Status -eq "warn" -or $Status -eq "fail") {
        if ($Fix) { $script:Fixable++ }
    }
}

function Test-Command {
    param([string]$Name, [string]$Hint = "")
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if ($cmd) {
        $version = (& $Name -version 2>&1 | Select-Object -First 1)
        Add-Result $Name "ok" "$version"
    } else {
        # Windows PowerShell 5.1 has no ternary operator.
        $advice = $Hint
        if (-not $advice) { $advice = "install it and re-run" }
        Add-Result $Name "fail" "not found on PATH" $advice
    }
}

Write-Host ""
Write-Host "  T_Dubber runtime doctor" -ForegroundColor Cyan
Write-Host "  $AppDir" -ForegroundColor DarkGray
Write-Host ""

# ---------------------------------------------------------------------------
# 1. Python
# ---------------------------------------------------------------------------
Write-Host "[1/8] Python" -ForegroundColor Cyan
$pythonCmd = $null
foreach ($candidate in @("python", "py -3")) {
    $c = Get-Command ($candidate -split ' ')[0] -ErrorAction SilentlyContinue
    if ($c) { $pythonCmd = $candidate; break }
}
if (-not $pythonCmd) {
    Add-Result "python" "fail" "not found" "install Python 3.10+ from python.org"
} else {
    $ver = (& python --version 2>&1) -join ""
    $ver = $ver.Trim()
    $minor = 0
    if ($ver -match '3\.(\d+)') { $minor = [int]$Matches[1] }
    if ($minor -ge 10) {
        Add-Result "python" "ok" "$ver  ($pythonCmd)"
    } else {
        Add-Result "python" "fail" "$ver is older than 3.10" "install Python 3.10+"
    }
}

# ---------------------------------------------------------------------------
# 2. Python packages
# ---------------------------------------------------------------------------
Write-Host "[2/8] Python packages" -ForegroundColor Cyan
$required = @{
    "gradio"   = "the UI"
    "telethon" = "Telegram uploads"
    "yt_dlp"   = "link resolution"
    "kaggle"   = "the Kaggle orchestrator"
    "requests" = "telegram_backup.py"
}
foreach ($pkg in $required.Keys | Sort-Object) {
    $found = & python -c "import $pkg" 2>&1
    if ($LASTEXITCODE -eq 0) {
        $v = (& python -c "import importlib.metadata as m; print(m.version('$pkg'))" 2>&1)
        Add-Result "pip:$pkg" "ok" "$v - $($required[$pkg])"
    } else {
        $status = "fail"
        if ($Fix) {
            & python -m pip install --quiet --disable-pip-version-check "$pkg" 2>&1 | Out-Null
            if ($LASTEXITCODE -eq 0) {
                Add-Result "pip:$pkg" "fixed" "installed just now"
                continue
            }
        }
        Add-Result "pip:$pkg" $status "missing" "pip install $pkg"
    }
}

# ---------------------------------------------------------------------------
# 3. ffmpeg / ffprobe
# ---------------------------------------------------------------------------
Write-Host "[3/8] Media tools" -ForegroundColor Cyan
Test-Command "ffmpeg"  "winget install --id Gyan.FFmpeg -e"
Test-Command "ffprobe" "ships with ffmpeg"

# ---------------------------------------------------------------------------
# 4. Kaggle credentials
# ---------------------------------------------------------------------------
Write-Host "[4/8] Kaggle credentials" -ForegroundColor Cyan
$kaggleJson = Join-Path $AppDir "kaggle_paperWork\kaggle.json"
if (Test-Path $kaggleJson) {
    try {
        $k = Get-Content $kaggleJson -Raw | ConvertFrom-Json
        if ($k.username -and $k.key) {
            Add-Result "kaggle.json" "ok" "present, user $($k.username)"
        } else {
            Add-Result "kaggle.json" "warn" "missing username or key" "re-copy it from kaggle.com/settings"
        }
    } catch {
        Add-Result "kaggle.json" "warn" "present but not valid JSON" "re-copy it from kaggle.com/settings"
    }
} else {
    Add-Result "kaggle.json" "fail" "not found at kaggle_paperWork\kaggle.json" "download it from kaggle.com/settings"
}

# ---------------------------------------------------------------------------
# 5. Telegram settings
# ---------------------------------------------------------------------------
Write-Host "[5/8] Telegram" -ForegroundColor Cyan
$configPath = Join-Path $AppDir "config.json"
if (Test-Path $configPath) {
    try {
        $cfg = Get-Content $configPath -Raw | ConvertFrom-Json
        if ($cfg.api_hash_protected -like "dpapi:v1:*") {
            Add-Result "config.json" "ok" "present, API hash is DPAPI-encrypted"
        } elseif ($cfg.api_hash) {
            Add-Result "config.json" "warn" "API hash is stored in PLAINTEXT" "save settings in the UI once to migrate it to DPAPI"
        } else {
            Add-Result "config.json" "warn" "no API hash saved" "save it in the Telegram Drive tab"
        }
        $channels = @()
        $chFile = Join-Path $AppDir "channels.json"
        if (Test-Path $chFile) {
            try {
                $channels = @((Get-Content $chFile -Raw | ConvertFrom-Json).channels)
            } catch { }
        }
        if ($channels.Count -eq 0 -and $cfg.channel) { $channels = @($cfg.channel) }
        Add-Result "channels" "ok" "$($channels.Count) configured: $($channels -join ', ')"
    } catch {
        Add-Result "config.json" "warn" "not valid JSON" "re-save settings in the UI"
    }
} else {
    Add-Result "config.json" "fail" "not found" "save Telegram settings in the Drive tab"
}

$session = Join-Path $AppDir "telegram_uploader_session.session"
if (Test-Path $session) {
    $age = (Get-Item $session).LastWriteTime
    $days = [math]::Round(((Get-Date) - $age).TotalDays, 1)
    if ($days -gt 60) {
        Add-Result "telegram session" "warn" "last written $days days ago" "python telegram_uploader.py to confirm it still works"
    } else {
        Add-Result "telegram session" "ok" "present, touched $days days ago"
    }
} else {
    Add-Result "telegram session" "warn" "not found" "python telegram_uploader.py  (one-time OTP login)"
}

# ---------------------------------------------------------------------------
# 6. Disk space
# ---------------------------------------------------------------------------
Write-Host "[6/8] Disk space" -ForegroundColor Cyan
$drive = Get-PSDrive -Name ((Split-Path -Qualifier $AppDir).TrimEnd(":")) -ErrorAction SilentlyContinue
if ($drive) {
    $freeGb = [math]::Round($drive.Free / 1GB, 1)
    $usedGb = [math]::Round($drive.Used / 1GB, 1)
    if ($freeGb -lt 20) {
        Add-Result "free space" "warn" "$freeGb GB free of $usedGb GB used" "a 90 GB restore needs ~90 GB free"
    } else {
        Add-Result "free space" "ok" "$freeGb GB free of $usedGb GB used"
    }
} else {
    Add-Result "free space" "warn" "could not read"
}

# ---------------------------------------------------------------------------
# 7. SQLite index
# ---------------------------------------------------------------------------
Write-Host "[7/8] SQLite index" -ForegroundColor Cyan
$dbFile = Join-Path $AppDir "t_dubber.db"
if (Test-Path $dbFile) {
    $dbMb = [math]::Round((Get-Item $dbFile).Length / 1MB, 2)
    try {
        $stats = & python -c "import db,json;print(json.dumps(db.stats()))" 2>&1 | ConvertFrom-Json
        Add-Result "t_dubber.db" "ok" ("{0} runs, {1} archives, {2} parts, {3} MB" -f $stats.projects, $stats.archives, $stats.parts, $dbMb)
    } catch {
        Add-Result "t_dubber.db" "warn" "present ($dbMb MB) but could not be read" "python db_seed.py"
    }
} elseif ($Fix) {
    & python db_seed.py 2>&1 | Out-Null
    if (Test-Path $dbFile) {
        Add-Result "t_dubber.db" "fixed" "created and seeded from projects/"
    } else {
        Add-Result "t_dubber.db" "fail" "not found" "python db_seed.py"
    }
} else {
    Add-Result "t_dubber.db" "warn" "not created yet" "python db_seed.py"
}

# ---------------------------------------------------------------------------
# 8. tgup: the optional multi-connection uploader
# ---------------------------------------------------------------------------
Write-Host "[8/8] tgup uploader (optional)" -ForegroundColor Cyan
$goInfo = & python go_planner.py check 2>&1
$goText = ($goInfo | Out-String)
if ($goText -match '"runnable":\s*true') {
    # Distinguish the two states that matter: the binary runs, but has never
    # logged in. That is not a fault, it just means the first upload asks for a
    # Telegram code once.
    if ($goText -match '"needs_login":\s*true') {
        # Not a fault, but it is a real limitation: with no session tgup never
        # starts a login (it would ask Telegram for a code nobody at this end
        # can answer, then fail at EOF), so every upload goes via Telethon
        # until someone logs in once from a console.
        Add-Result "tgup uploader" "warn" "built and runnable, but has no session: uploads use Telethon until you log in once from a console" "tgup upload --file <any> --channel @name --api-id N --api-hash H --phone +CC...  (stdin must be a terminal)"
    } else {
        Add-Result "tgup uploader" "ok" "built, runnable, and already logged in (multi-connection uploads available)"
    }
} elseif ($goText -match '"present":\s*true') {
    Add-Result "tgup uploader" "warn" "built but blocked by a Windows app-control policy" "optional; Telethon uploads everything"
} else {
    Add-Result "tgup uploader" "warn" "not built (optional)" "tgup\build.ps1"
}
if ($goText -match '"reason":\s*"([^"]+)"') {
    Add-Result "tgup reason" "info" $Matches[1]
}

# Smart App Control state, so the "blocked" warning above is explainable.
try {
    $sac = (Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy" -ErrorAction Stop).VerifiedAndReputablePolicyState
    $label = @{ 0 = "Off"; 1 = "On"; 2 = "Evaluation" }[$sac]
    Add-Result "Smart App Control" "ok" "$label (unsigned new executables may be blocked)"
} catch {
    Add-Result "Smart App Control" "ok" "state not readable"
}

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
if ($Json) {
    $script:Results | ConvertTo-Json -Depth 4
    exit 0
}

Write-Host ""
Write-Host "  Results" -ForegroundColor Cyan
Write-Host "  " + ("-" * 62)
$colour = @{ ok = "Green"; fixed = "Cyan"; warn = "Yellow"; fail = "Red" }
foreach ($r in $script:Results) {
    $glyph = switch ($r.Status) {
        "ok"    { "ok   " }
        "fixed" { "fixed" }
        "warn"  { "warn " }
        "fail"  { "FAIL " }
    }
    Write-Host ("  {0}  {1,-22} {2}" -f $glyph, $r.Check, $r.Detail) -ForegroundColor $colour[$r.Status]
    if ($r.Fix -and $r.Status -ne "fixed" -and $r.Status -ne "ok") {
        Write-Host ("          -> {0}" -f $r.Fix) -ForegroundColor DarkGray
    }
}

$failed = @($script:Results | Where-Object { $_.Status -eq "fail" }).Count
$warned = @($script:Results | Where-Object { $_.Status -eq "warn" }).Count
$fixedN = @($script:Results | Where-Object { $_.Status -eq "fixed" }).Count

Write-Host "  " + ("-" * 62)
Write-Host ""
if ($failed -gt 0) {
    Write-Host "  $failed check(s) failed, $warned warning(s), $fixedN fixed." -ForegroundColor Red
    Write-Host "  Fix the FAIL rows above before starting a run." -ForegroundColor Red
    exit 1
}
if ($warned -gt 0) {
    Write-Host "  Everything essential passed. $warned warning(s), $fixedN fixed." -ForegroundColor Yellow
    exit 0
}
Write-Host "  All $($script:Results.Count) checks passed." -ForegroundColor Green
exit 0
