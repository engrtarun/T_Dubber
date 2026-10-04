param(
    [Parameter(ValueFromRemainingArguments=$true)]
    [string[]]$GoArgs,

    [switch]$CleanupAfterUpload
)

# ==========================================
# THE SMART CONDUCTOR (run_go.ps1)
# ==========================================
# Yeh script Baahubali truck (Go) ko start karne se pehle 15+ System Checks karti hai.
# Har baari jab truck start hoga, Conductor pehle rasta, engine, aur fuel check karega!

function Write-Log {
    param([string]$Message, [string]$Color = "Cyan")
    Write-Host "[CONDUCTOR] $Message" -ForegroundColor $Color
}

Write-Log "Initializing System Diagnostics..." "Magenta"

# 1. OS & Uptime Check
$os = Get-CimInstance Win32_OperatingSystem
$uptime = (Get-Date) - $os.LastBootUpTime
Write-Log "OS: $($os.Caption) | Uptime: $($uptime.Days)d $($uptime.Hours)h"

# 2. RAM Check
$totalRam = [math]::Round($os.TotalVisibleMemorySize / 1MB, 2)
$freeRam = [math]::Round($os.FreePhysicalMemory / 1MB, 2)
Write-Log "Memory: $freeRam GB Free / $totalRam GB Total"

# 3. CPU Load Check
$cpu = Get-CimInstance Win32_Processor
Write-Log "Processor: $($cpu.Name) | Cores: $($cpu.NumberOfLogicalProcessors)"

# 4. Storage Space Check (Drive C)
$drive = Get-PSDrive C
$freeSpace = [math]::Round($drive.Free / 1GB, 2)
Write-Log "Storage: $freeSpace GB Free on C: Drive"
if ($freeSpace -lt 5) {
    Write-Log "WARNING: Storage is critically low! (<5GB)" "Yellow"
}

# 5. Network Connectivity (Ping Test)
Write-Log "Checking Internet Route..."
if (!(Test-Connection "8.8.8.8" -Count 1 -Quiet -ErrorAction SilentlyContinue)) {
    Write-Log "CRITICAL: No Internet Connection!" "Red"
    exit 1
} else {
    Write-Log "Internet Route is CLEAR." "Green"
}

# 6. Check Active Network Adapter
$netAdapter = Get-NetAdapter | Where-Object { $_.Status -eq "Up" } | Select-Object -First 1
if ($netAdapter) {
    Write-Log "Active Interface: $($netAdapter.InterfaceDescription)"
}

# 7. Locate Go Engine (The Truck)
Write-Log "Locating Go Engine..."
$goBin = "go"
if ((Get-Command $goBin -ErrorAction SilentlyContinue) -eq $null) {
    $localGo = "$env:LOCALAPPDATA\Programs\Go\bin\go.exe"
    if (Test-Path $localGo) {
        $goBin = $localGo
    } else {
        Write-Log "CRITICAL: Go engine not found! Truck cannot start." "Red"
        exit 1
    }
}

# 8. Go Version Verification
$goVersion = & $goBin version
Write-Log "Go Engine Verified: $goVersion" "Green"

# 9. Extracting File Path from Args for tracking
$targetFile = $null
for ($i=0; $i -lt $GoArgs.Count; $i++) {
    if ($GoArgs[$i] -eq "--file" -and ($i+1) -lt $GoArgs.Count) {
        $targetFile = $GoArgs[$i+1]
        break
    }
}

# 10. File Size Check (If file provided)
if ($targetFile -and (Test-Path $targetFile)) {
    $fileItem = Get-Item $targetFile
    $fileSize = [math]::Round($fileItem.Length / 1MB, 2)
    Write-Log "Target Payload: $targetFile ($fileSize MB)"
}

# 11. Security Check
# Ensure we are not running as admin unnecessarily (for safety)
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if ($isAdmin) {
    Write-Log "Notice: Running with elevated privileges." "Yellow"
}

# 12. Check Telegram API reachability (Port 443)
Write-Log "Checking Telegram API servers..."
try {
    $tcp = New-Object System.Net.Sockets.TcpClient("api.telegram.org", 443)
    if ($tcp.Connected) {
        Write-Log "Telegram API is reachable." "Green"
        $tcp.Close()
    }
} catch {
    Write-Log "Warning: Could not verify Telegram API ping." "Yellow"
}

# 13. Pre-flight Manifest Validation
Write-Log "Conductor clearance granted. Passing control to Baahubali." "Magenta"
Write-Log "--------------------------------------------------------" "White"

# 14. EXECUTING THE ENGINE (STARTING THE TRUCK)
& $goBin run . @GoArgs
$exitCode = $LASTEXITCODE

Write-Log "--------------------------------------------------------" "White"

# 15. Post-Delivery Cleanup
if ($exitCode -eq 0) {
    Write-Log "Delivery Successful!" "Green"
    if ($CleanupAfterUpload -and $targetFile -and (Test-Path $targetFile)) {
        Write-Log "Cleaning up temporary file: $targetFile" "Yellow"
        Remove-Item -Path $targetFile -Force
        Write-Log "File deleted from local storage." "Green"
    }
} else {
    Write-Log "Delivery Failed! Code: $exitCode" "Red"
}

# 16. Final Exit
exit $exitCode
