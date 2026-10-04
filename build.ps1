<#
.SYNOPSIS
    Build the tgup uploader.

.DESCRIPTION
    Compiles tgup from source into tgup.exe next to the Go files.

    The Go toolchain is not always on PATH -- on this machine it lives under
    %LOCALAPPDATA%\Programs\Go -- so this script looks for it in the usual
    places before giving up, rather than asking you to fix your PATH first.

.PARAMETER Clean
    Delete the binary and the per-connection session copies before building.

.PARAMETER Vet
    Also run `go vet`, which is slower but catches things the compiler allows.

.EXAMPLE
    .\build.ps1
    .\build.ps1 -Clean -Vet
#>
[CmdletBinding()]
param(
    [switch]$Clean,
    [switch]$Vet
)

$ErrorActionPreference = 'Stop'

$here = Split-Path -Parent $MyInvocation.MyCommand.Path

function Find-Go {
    <#
        Resolve the go.exe path. Explicit PATH entries win; then the standard
        install location; then whatever the registry says.
    #>
    $onPath = Get-Command go.exe -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }

    $candidates = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Go\bin\go.exe'),
        (Join-Path $env:ProgramFiles 'Go\bin\go.exe')
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path $candidate)) { return $candidate }
    }

    $key = 'HKLM:\SOFTWARE\GoProgrammingLanguage'
    if (Test-Path $key) {
        $root = (Get-ItemProperty $key -ErrorAction SilentlyContinue).InstallLocation
        if ($root) {
            $candidate = Join-Path $root 'bin\go.exe'
            if (Test-Path $candidate) { return $candidate }
        }
    }
    return $null
}

$go = Find-Go
if (-not $go) {
    Write-Host 'Go toolchain not found.' -ForegroundColor Red
    Write-Host 'Install it from https://go.dev/dl/ then re-run this script.'
    exit 2
}
Write-Host "go        $go"
& $go version

# A build should not inherit a half-finished state from the last run.
$binary = Join-Path $here 'tgup.exe'
if ($Clean -and (Test-Path $binary)) {
    Write-Host 'clean     removing tgup.exe'
    Remove-Item $binary -Force
}
Get-ChildItem -Path $here -Filter 'tgup.session.conn*' -ErrorAction SilentlyContinue |
    ForEach-Object {
        Write-Host "clean     removing $($_.Name)"
        Remove-Item $_.FullName -Force
    }

$env:GOFLAGS = '-mod=mod'
if (-not $env:GOPATH) {
    $env:GOPATH = Join-Path $HOME 'go'
    Write-Host "GOPATH    $env:GOPATH (was unset)"
}

Push-Location $here
try {
    if ($Vet) {
        Write-Host "`nvet       running go vet"
        & $go vet ./...
        if ($LASTEXITCODE -ne 0) {
            Write-Host 'go vet reported problems; not building.' -ForegroundColor Red
            exit 1
        }
    }

    Write-Host "`nbuild     compiling"
    & $go build -o tgup.exe .
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'build failed.' -ForegroundColor Red
        exit 1
    }
}
finally {
    Pop-Location
}

if (-not (Test-Path $binary)) {
    Write-Host 'build reported success but produced no binary.' -ForegroundColor Red
    exit 1
}

$size = [math]::Round((Get-Item $binary).Length / 1MB, 2)
Write-Host "`nbuilt     tgup.exe ($size MB)"

# Prove it starts. A binary that compiles but will not launch is exactly the
# failure this project already hit once, so it is worth checking every time.
$help = & $binary help 2>&1
if ($LASTEXITCODE -eq 0 -and ($help -join "`n") -match 'tgup') {
    Write-Host 'verified  tgup.exe starts and reports help' -ForegroundColor Green
    Write-Host ''
    Write-Host "First upload will ask for a Telegram login code once, then"
    Write-Host "reuse the session in $here\tgup.session"
    Write-Host ''
    Write-Host 'Measure whether concurrency actually helps:'
    Write-Host "  ..\tgup_bridge.py bench --channel @yourchannel"
}
else {
    Write-Host 'the binary built but did not run.' -ForegroundColor Red
    Write-Host 'Smart App Control may be blocking it. Everything keeps working'
    Write-Host 'through Telethon regardless.'
    exit 1
}