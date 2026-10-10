# Build tdub_tts, retrying through the Windows `os error 32` file-lock race.
#
# WHY THIS EXISTS
# ---------------
# On this box (cargo/rustc 1.99.0, x86_64-pc-windows-msvc) a cold release build
# of the candle stack fails intermittently with:
#
#   error: failed to remove/write target\release\deps\<crate>-<hash>...
#   .rcgu.o / .rmeta: The process cannot access the file because it is being
#   used by another process. (os error 32)
#
# Reproduced 2026-10-10 against the full dep tree with no `panic = "abort"` and
# no `codegen-units = 1`: `libgemm_c32-*.rmeta` on one attempt,
# `darling_core-*-cgu.05.rcgu.o` on the next. A different file every time is
# the signature of a lock race, not of a profile option -- and a later attempt
# of the identical manifest built clean. So: RETRY, do not bisect Cargo.toml.
#
# An earlier note in Cargo.toml blamed `panic = "abort"` for this. That was a
# misattribution and has been corrected.
#
# Usage:  .\build_release.ps1 [-Attempts 8]
# Exit:   0 on success, 1 if every attempt failed.

param(
    [int]$Attempts = 8
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
Push-Location $root
try {
    $binary = Join-Path $root 'target\release\tdub_tts.exe'
    $succeeded = $false
    $t0all = Get-Date

    for ($i = 1; $i -le $Attempts -and -not $succeeded; $i++) {
        $t0 = Get-Date
        Write-Host "=== ATTEMPT $i/$Attempts $(Get-Date -Format 'HH:mm:ss') ===" -ForegroundColor Cyan
        # --message-format short keeps the dep-tree scroll readable.
        cargo build --release --message-format short 2>&1 |
            ForEach-Object { Write-Host "  $_" }
        $code = $LASTEXITCODE
        $el = [int]((Get-Date) - $t0).TotalSeconds

        if ($code -eq 0 -and (Test-Path $binary)) {
            Write-Host "SUCCESS attempt=$i elapsed=${el}s" -ForegroundColor Green
            $succeeded = $true
        }
        else {
            # 101 is rustc's generic failure; os error 32 lands inside it.
            Write-Host "FAIL attempt=$i exit=$code elapsed=${el}s" -ForegroundColor Yellow
            Start-Sleep -Seconds 3
        }
    }

    if (-not $succeeded) {
        Write-Host "ALL_ATTEMPTS_FAILED after $([int]((Get-Date) - $t0all).TotalSeconds)s" -ForegroundColor Red
        exit 1
    }

    # Report the artefact, then prove it actually runs -- a successful link
    # says nothing about a working binary, and this build's whole purpose is
    # the CPU-only guarantee below.
    $info = Get-Item $binary
    Write-Host ("BUILT {0}  {1:N1} MB" -f $info.Name, ($info.Length / 1MB))

    & $binary --help 2>&1 | Select-Object -First 3 | ForEach-Object { Write-Host "  $_" }
    if ($LASTEXITCODE -ne 0) { Write-Host "--help failed (exit $LASTEXITCODE)" -ForegroundColor Red; exit 1 }

    # The load-bearing assertion: cuda must not compile in.
    $probe = & $binary --device cuda --model . --text x --out x.wav 2>&1
    if ($LASTEXITCODE -eq 2 -and ($probe -match 'CPU-only')) {
        Write-Host "CPU-ONLY OK: --device cuda rejected (exit 2)" -ForegroundColor Green
    }
    else {
        Write-Host "CPU-ONLY CHECK FAILED exit=$LASTEXITCODE: $probe" -ForegroundColor Red
        exit 1
    }

    Write-Host "BUILD_AND_VERIFY_OK" -ForegroundColor Green
    exit 0
}
finally {
    Pop-Location
}
