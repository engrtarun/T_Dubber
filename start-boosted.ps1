# T_Dubber Boosted Launcher - NO ADMIN, ZERO system-wide changes
# Bas ye chalao: python resolve hota hai, free port milta hai, app.py log ke
# saath start hota hai, aur health check pass hone par banner + browser khulta.
#
# ZERO-CHANGE POLICY (AI/dev note): this launcher intentionally makes ZERO
# system-wide changes - no registry writes, no netsh, no powercfg, no firewall
# rules, no scheduled tasks, no touching other processes, and it never asks for
# Administrator. The only "boost" is process-local: the single python child it
# spawns gets AboveNormal priority plus a CPU affinity mask computed from the
# real logical-CPU count, and all diagnostics go to repo-local files under
# logs\. Why: the old launcher tuned the whole machine on every run, and those
# knobs (HKLM NetworkThrottlingIndex / SystemResponsiveness, firewall rules,
# power schemes, other apps' priorities) cannot be safely undone by a non-admin
# script - so this one must be runnable any number of times with no side
# effects outside its own child process. To undo the earlier machine-wide
# tweaks exactly once:   .\revert-boost.ps1 -Elevate -Execute
# (plain `.\revert-boost.ps1` = dry-run plan, changes nothing). boost.ps1 and
# install-autoboost.ps1 are now standalone, opt-in tools this launcher never
# calls; auto.py (Python) still self-elevates and re-applies HKLM/netsh tweaks
# with no revert path - do not run it if you want a clean machine.

[CmdletBinding()]
param(
    # Health-check budget: how long to wait for the Gradio URL to answer.
    [int]$HealthTimeoutSeconds = 120
)

$ErrorActionPreference = 'Stop'

$APP_DIR = $PSScriptRoot
if (-not $APP_DIR) { $APP_DIR = 'C:\Users\pocot\Music\T_Dubber' }
$APP_SCRIPT = 'app.py'
$BASE_PORT = 7860

# ============================================================
# PYTHON RESOLUTION (never trust the Microsoft Store stub)
# ============================================================

function Test-RealPython {
    param([string]$ExePath)

    if ([string]::IsNullOrWhiteSpace($ExePath)) { return $false }
    $ExePath = $ExePath.Trim().Trim('"')
    if (-not (Test-Path -LiteralPath $ExePath)) { return $false }

    # C:\...\WindowsApps\python.exe is the Microsoft Store alias stub: it
    # exists on PATH, opens the Store / exits instantly with code 9009 and
    # prints nothing -> dead PID + fake success banner. Never accept it.
    if ($ExePath -like '*\WindowsApps\*') { return $false }

    # Actually RUN the candidate: a broken shim must not pass this gate.
    # (stdout only - do not redirect stderr here, $ErrorActionPreference=Stop
    # turns redirected native stderr into a terminating error in PS 5.1.)
    try {
        $out = & $ExePath -c "import sys; print('PYVER %d.%d.%d' % sys.version_info[:3])" | Out-String
        if ($LASTEXITCODE -ne 0) { return $false }
        return ($out -match 'PYVER \d+\.\d+\.\d+')
    } catch {
        return $false
    }
}

function Resolve-PythonExe {
    # 1) Every 'python'/'python3' application on PATH, Store stub excluded.
    foreach ($name in @('python', 'python3')) {
        $cmds = @(Get-Command $name -CommandType Application -All -ErrorAction SilentlyContinue)
        foreach ($cmd in $cmds) {
            if ($cmd.Source -like '*\WindowsApps\*') { continue }  # Store stub
            if (Test-RealPython -ExePath $cmd.Source) { return $cmd.Source }
        }
    }

    # 2) Fallback: the py launcher picks a real interpreter, we take its path.
    $py = Get-Command py -CommandType Application -ErrorAction SilentlyContinue
    if ($py) {
        try {
            $exe = (& $py.Source -3 -c "import sys; print(sys.executable)" | Out-String).Trim()
            if ($LASTEXITCODE -eq 0 -and (Test-RealPython -ExePath $exe)) { return $exe }
        } catch {
            # ignore - fall through to the throw below
        }
    }

    throw ("No working Python 3 found. Install it from https://www.python.org/downloads/ " +
        "and tick 'Add python.exe to PATH'. Looked at: PATH 'python' " +
        "(Microsoft Store stub excluded), 'python3', then 'py -3'.")
}

# ============================================================
# PORT SELECTION (app.py does not pin a port - avoid clashes)
# ============================================================

function Test-PortListening {
    param([int]$Port)
    try {
        return [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    } catch {
        return $false
    }
}

function Select-GradioPort {
    param(
        [int]$StartPort = 7860,
        [int]$Tries = 40
    )
    for ($p = $StartPort; $p -lt ($StartPort + $Tries); $p++) {
        if (-not (Test-PortListening -Port $p)) { return $p }
        Write-Host "  [!] Port $p already in use - trying the next one" -ForegroundColor Yellow
    }
    throw "No free TCP port found in $StartPort..$($StartPort + $Tries - 1)."
}

function Test-UrlAlive {
    param(
        [string]$Url,
        [int]$TimeoutMs = 1500
    )
    try { $uri = [System.Uri]$Url } catch { return $false }
    if (-not $uri.IsAbsoluteUri) { return $false }

    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $task = $client.ConnectAsync($uri.Host, $uri.Port)
        $done = $task.Wait($TimeoutMs)     # throws on connection-refused
        return ($done -and $client.Connected)
    } catch {
        return $false
    } finally {
        $client.Close()
        $client.Dispose()
    }
}

# Gradio prints "Running on local URL:  http://127.0.0.1:<port>" when it is
# really up. app.py never pins a port, so trust the log over our guess.
function Get-ReportedUrl {
    param([string[]]$LogFiles)
    foreach ($f in $LogFiles) {
        if (-not $f -or -not (Test-Path -LiteralPath $f)) { continue }
        try {
            $hit = Select-String -Path $f -Pattern 'Running on local URL:\s+(\S+)' -ErrorAction SilentlyContinue |
                Select-Object -Last 1
        } catch {
            $hit = $null
        }
        if ($hit -and $hit.Matches.Count -gt 0) { return $hit.Matches[0].Groups[1].Value }
    }
    return $null
}

function Show-LogTail {
    param(
        [string[]]$LogFiles,
        [int]$Lines = 40
    )
    foreach ($f in $LogFiles) {
        if (-not $f -or -not (Test-Path -LiteralPath $f)) { continue }
        try { $tail = @(Get-Content -LiteralPath $f | Select-Object -Last $Lines) }
        catch { $tail = @() }
        if ($tail.Count -eq 0) { continue }
        $leaf = Split-Path -Leaf $f
        Write-Host ""
        Write-Host "  ---- last $($tail.Count) line(s) of $leaf ----" -ForegroundColor DarkGray
        foreach ($line in $tail) { Write-Host "  | $line" -ForegroundColor DarkGray }
    }
}

# Never claim success we did not verify: reason + exit code + .err tail, exit 1.
function Show-LaunchFailure {
    param(
        [string]$Reason,
        $ChildExitCode,           # [int] or $null when the code could not be read
        [string[]]$LogFiles,
        [int]$ExitCode = 1
    )
    Write-Host ""
    Write-Host "  [X] T_Dubber launcher FAILED: $Reason" -ForegroundColor Red
    if ($null -ne $ChildExitCode) {
        Write-Host "  [X] python exit code: $ChildExitCode" -ForegroundColor Red
    } else {
        Write-Host "  [X] python exit code: unknown (could not be read)" -ForegroundColor Red
    }
    Show-LogTail -LogFiles $LogFiles -Lines 40
    Write-Host ""
    exit $ExitCode
}

# Windows PowerShell 5.1 quirk (verified): when Start-Process uses
# -RedirectStandardOutput/-RedirectStandardError the returned Process reports
# ExitCode=$null and Handle="" once the child is gone (without redirection it
# works). So the raw handle is cached right after launch and the code is read
# from kernel32 instead - never trust a banner we cannot verify.
function Get-ChildExitCode {
    param(
        [System.Diagnostics.Process]$Proc,
        [IntPtr]$CachedHandle = [IntPtr]::Zero
    )

    # Path 1: the managed property (works on PS7 / non-redirected launches)
    try {
        $code = $Proc.ExitCode
        if ($null -ne $code) { return [int]$code }
    } catch { }

    # Path 2: cached raw handle + kernel32!GetExitCodeProcess
    if ($CachedHandle -ne [IntPtr]::Zero) {
        try {
            if (-not ('TDDubber.Native' -as [type])) {
                Add-Type -Namespace TDDubber -Name Native -MemberDefinition @'
[DllImport("kernel32.dll")]
public static extern bool GetExitCodeProcess(IntPtr hProcess, out uint lpExitCode);
'@
            }
            $raw = [uint32]0
            if ([TDDubber.Native]::GetExitCodeProcess($CachedHandle, [ref]$raw) -and $raw -ne 259) {
                return [int]$raw
            }
        } catch { }
    }
    return $null
}

# ============================================================
# PROCESS-LOCAL BOOST (this child ONLY - nothing system-wide)
# ============================================================

function Set-ChildBoost {
    param([System.Diagnostics.Process]$Proc)

    $cpuCount = [Environment]::ProcessorCount

    # Affinity mask from the REAL core count. This box has 8 logical CPUs ->
    # 0xFF. (The old hardcoded 0xFFFF meant CPUs 8-15, which do not exist
    # here, so it always failed and was swallowed by catch {}.)
    if ($cpuCount -ge 63) { $mask = [int64]::MaxValue }
    else { $mask = [int64]((1L -shl $cpuCount) - 1) }

    $Proc.Refresh()
    if ($Proc.HasExited) {
        throw "python exited immediately (before it could even start) - see the log files"
    }

    # Process-local only: priority + affinity of OUR child. No registry, no
    # netsh, no powercfg, no firewall, no scheduled tasks, no other processes.
    $Proc.PriorityClass = 'AboveNormal'
    $Proc.ProcessorAffinity = [IntPtr]$mask

    return @{ CpuCount = $cpuCount; Mask = $mask }
}

# Test hook: dot-sourcing (`. .\start-boosted.ps1`) or TDUBBER_LIB_ONLY=1
# loads ONLY the functions above - the app is never launched. Used by
# validation to exercise the python-resolution and port-check logic.
if ($MyInvocation.InvocationName -eq '.' -or $env:TDUBBER_LIB_ONLY -eq '1') { return }

# ============================================================
# MAIN
# ============================================================

try {
    if (-not (Test-Path -LiteralPath (Join-Path $APP_DIR $APP_SCRIPT))) {
        throw "$APP_SCRIPT not found in $APP_DIR"
    }

    # --- python (verified by running it, Store stub excluded) ---
    $python = Resolve-PythonExe
    Write-Host "  [snake] Python: $python" -ForegroundColor Cyan

    # --- free port for Gradio (app.py does not pin one) ---
    $port = Select-GradioPort -StartPort $BASE_PORT
    if ($port -eq $BASE_PORT) {
        Write-Host "  [net] Port: $port (free)" -ForegroundColor Cyan
    } else {
        Write-Host "  [net] Port: $port - GRADIO_SERVER_PORT will pin Gradio here (7860 was busy)" -ForegroundColor Yellow
    }

    # --- repo-local logs (app.py writes no log file of its own) ---
    $logDir = Join-Path $APP_DIR 'logs'
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $outLog = Join-Path $logDir "app-$stamp.log"
    $errLog = Join-Path $logDir "app-$stamp.err"
    Write-Host "  [doc] stdout: $outLog" -ForegroundColor DarkGray
    Write-Host "  [doc] stderr: $errLog" -ForegroundColor DarkGray

    # Gradio honours these env vars; app.py calls demo.launch() with no port.
    $env:PYTHONUNBUFFERED = '1'
    $env:GRADIO_SERVER_NAME = '127.0.0.1'
    $env:GRADIO_SERVER_PORT = "$port"

    # --- launch with redirection so tracebacks are actually visible ---
    try {
        $proc = Start-Process -FilePath $python -ArgumentList @($APP_SCRIPT) `
            -WorkingDirectory $APP_DIR -PassThru -WindowStyle Hidden `
            -RedirectStandardOutput $outLog -RedirectStandardError $errLog
    } catch {
        throw "could not start python: $($_.Exception.Message)"
    }

    # Cache the RAW handle NOW: after a redirected child exits, PS 5.1 gives
    # ExitCode=null / Handle="" (see Get-ChildExitCode). Only the cached
    # handle still yields the real exit code later.
    $childHandle = [IntPtr]::Zero
    try {
        $h = $proc.Handle
        if ($h) { $childHandle = [IntPtr]$h }
    } catch { }

    # --- boost: this child only, computed from the real CPU count ---
    try {
        $boost = Set-ChildBoost -Proc $proc
    } catch {
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        throw "process-local boost failed: $($_.Exception.Message)"
    }
    $maskHex = '0x{0:X}' -f $boost.Mask
    Write-Host "  [lightning] PID $($proc.Id): AboveNormal, affinity $maskHex ($($boost.CpuCount) logical CPUs) - process-local only" -ForegroundColor Green

    # --- REAL health check: process exit OR answered socket, up to 120s ---
    Write-Host "  [hourglass] Waiting for Gradio (up to $HealthTimeoutSeconds s)..." -ForegroundColor Gray
    $deadline = (Get-Date).AddSeconds($HealthTimeoutSeconds)
    $aliveUrl = $null
    $urlReported = $false

    while ((Get-Date) -lt $deadline) {
        if ($proc.HasExited) {
            $childExit = Get-ChildExitCode -Proc $proc -CachedHandle $childHandle
            Show-LaunchFailure -Reason 'python died during startup (no health check to pass)' `
                -ChildExitCode $childExit -LogFiles @($errLog, $outLog)
        }

        $reported = Get-ReportedUrl -LogFiles @($outLog, $errLog)
        if ($reported -and -not $urlReported) {
            Write-Host "  [ear] Gradio reported: $reported" -ForegroundColor Gray
            $urlReported = $true
        }

        # Trust the URL Gradio printed (handles port drift), else our pick.
        if ($reported) { $probe = $reported } else { $probe = "http://127.0.0.1:$port" }
        if (Test-UrlAlive -Url $probe) {
            # The guessed port can answer milliseconds BEFORE the log line is
            # written, so scrape ONE more time and prefer Gradio's own URL if
            # it is there now (closes the port-drift race for good).
            $finalReported = Get-ReportedUrl -LogFiles @($outLog, $errLog)
            if ($finalReported -and -not $urlReported) {
                Write-Host "  [ear] Gradio reported: $finalReported" -ForegroundColor Gray
            }
            if ($finalReported -and (Test-UrlAlive -Url $finalReported)) {
                $aliveUrl = $finalReported
            } else {
                $aliveUrl = $probe
            }
            break
        }

        Start-Sleep -Milliseconds 750
    }

    if (-not $aliveUrl) {
        Write-Host "  [hourglass] Timeout: no HTTP answer within $HealthTimeoutSeconds s - stopping python." -ForegroundColor Yellow
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        $childExit = Get-ChildExitCode -Proc $proc -CachedHandle $childHandle
        Show-LaunchFailure -Reason 'Gradio never answered on any probed port' `
            -ChildExitCode $childExit -LogFiles @($errLog, $outLog)
    }

    # === ONLY now, after the socket answered, the success banner may print ===
    Write-Host ""
    Write-Host "==========================================================" -ForegroundColor Green
    Write-Host "  T_Dubber is UP - health check PASSED (socket answered)" -ForegroundColor Green
    Write-Host "==========================================================" -ForegroundColor Green
    Write-Host ""
    Write-Host "  [ok] URL:        $aliveUrl" -ForegroundColor Green
    Write-Host "  [ok] PID:        $($proc.Id)" -ForegroundColor Green
    Write-Host "  [ok] Boost:      AboveNormal, affinity $maskHex - process-local only, NO system-wide changes" -ForegroundColor Green
    Write-Host "  [ok] Python:     $python" -ForegroundColor Green
    Write-Host "  [doc] Logs:      $outLog" -ForegroundColor Gray
    Write-Host "  [doc]            $errLog" -ForegroundColor Gray
    Write-Host ""

    # app.py already passes demo.launch(inbrowser=True), so a tab may be open;
    # open the VERIFIED url too so the address is always reachable.
    Start-Process $aliveUrl

    Write-Host "  [stop] Close this window or press Ctrl+C to stop watching:" -ForegroundColor Yellow
    Write-Host "         Ctrl+C stops ONLY this launcher - T_Dubber PID $($proc.Id) keeps running" -ForegroundColor Yellow
    Write-Host "         in the background (stop it with:  Stop-Process -Id $($proc.Id) )." -ForegroundColor Yellow
    Write-Host "         Closing the window stops BOTH the launcher and T_Dubber." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  Waiting here - the app keeps serving until you stop it..." -ForegroundColor DarkGray

    # Ctrl+C during this interruptible sleep leaves $proc running (by design).
    while (-not $proc.HasExited) {
        Start-Sleep -Seconds 2
    }

    $finalExit = Get-ChildExitCode -Proc $proc -CachedHandle $childHandle
    Write-Host ""
    if ($null -eq $finalExit) {
        Write-Host "  T_Dubber (PID $($proc.Id)) exited with an UNKNOWN exit code." -ForegroundColor Yellow
        exit 1
    }
    Write-Host "  T_Dubber (PID $($proc.Id)) exited with code $finalExit." -ForegroundColor Yellow
    if ($finalExit -ne 0) { exit 1 }
    exit 0

} catch {
    Write-Host ""
    Write-Host "  [X] T_Dubber launcher FAILED: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
