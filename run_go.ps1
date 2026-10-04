param(
    [Parameter(ValueFromRemainingArguments=$true)]
    [string[]]$GoArgs
)

# PS1 Conductor Check: Find Go engine!
$goBin = "go"
if ((Get-Command $goBin -ErrorAction SilentlyContinue) -eq $null) {
    $localGo = "$env:LOCALAPPDATA\Programs\Go\bin\go.exe"
    if (Test-Path $localGo) {
        $goBin = $localGo
    } else {
        Write-Error "Go engine not found! Truck cannot start."
        exit 1
    }
}

# The Conductor (PS1) starts the Baahubali Truck (Go Engine)
Write-Host "Conductor (PS1) starting Go engine..." -ForegroundColor Cyan

# We use Start-Process or just run it directly.
# Since we need stdin, stdout, stderr to pipe seamlessly, we invoke it directly.
& $goBin run . @GoArgs
exit $LASTEXITCODE
