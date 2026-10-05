<#
.SYNOPSIS
    Build the Kaggle pack (five static Linux binaries) and export /pack to disk.

.DESCRIPTION
    Runs the Dockerfile's `pack-exporter` target and lands its /pack folder
    on this machine:

        pack\bin\{tgup, stitcher, normalizer, subtitle_forge, havaldar_core}
        pack\MANIFEST.txt
        pack\SHA256SUMS

    Why this shape:
      * BuildKit's `--output type=local` writes the image filesystem straight
        to a folder -- no container to start, nothing to extract by hand.
      * Only the builder stages run. The vLLM/torch "brain" stage (several GB
        and a long pull) is NOT a dependency of pack-exporter, so a rebuild
        costs minutes, not an afternoon.
      * Every binary is statically linked inside the build (musl for Rust,
        -static for C++, CGO_ENABLED=0 for Go) and the Dockerfile asserts it
        with `ldd` before the image even exists. This script re-asserts it on
        the exported bytes: ELF magic + SHA-256 match against SHA256SUMS.

    Fallback: on a Docker without the BuildKit buildx plugin, it builds the
    image normally and copies /pack out with `docker create` + `docker cp`.

.PARAMETER Dest
    Folder that will CONTAIN the exported pack\ directory.
    Default: the repo root (next to this script).
.PARAMETER Desktop
    Export to <Desktop>\T_Dubber_Kaggle_Pack\pack instead of the repo root.
.PARAMETER Image
    Tag for the intermediate pack image. Default: t-dubber-pack:latest.
.PARAMETER NoCache
    Pass --no-cache to docker build (full rebuild of every stage).
.PARAMETER KeepStaging
    Keep the temporary export folder (debugging aid).

.EXAMPLE
    .\build_kaggle_pack.ps1
    .\build_kaggle_pack.ps1 -Desktop
    .\build_kaggle_pack.ps1 -Dest D:\drops -NoCache

.NOTES
    After the export: upload `pack\` as a Kaggle dataset once (kaggle CLI
    needs %USERPROFILE%\.kaggle\kaggle.json), then attach that dataset to
    kaggle_worker.ipynb. Cell 0 already does the rest -- it extracts the
    pack to /kaggle/working/tdubber_pack, prepends its bin/ to PATH and pins
    NORMALIZER_BIN etc. with absolute paths. PATH alone is NOT enough:
    Kaggle's base image ships a foreign `normalizer` (argparse, `-t
    THRESHOLD`) that shadows the pack and silently drops the run onto the
    slow Python engine, which is why the worker verifies the pinned binary's
    CLI before proceeding. If you are doing this by hand instead, the
    equivalent setup is:

        import os, shutil, tarfile, zipfile
        # 1. extract the mounted pack to a writable dir, e.g. via the zip
        #    wrapper kaggle-tdubber-pack.zip -> kaggle-tdubber-pack.tar.gz
        # 2. chmod +x /kaggle/working/tdubber_pack/bin/*
        # 3. os.environ["NORMALIZER_BIN"] = ".../bin/normalizer"   (and
        #    STITCHER_BIN / TGUP_BIN / SUBTITLE_FORGE_BIN the same way)
        # 4. os.environ["PATH"] = ".../bin:" + os.environ["PATH"]

    No toolchain, no compilation, no network.
#>
[CmdletBinding()]
param(
    [string]$Dest = "",
    [switch]$Desktop,
    [string]$Image = "t-dubber-pack:latest",
    [switch]$NoCache,
    [switch]$KeepStaging
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = $PSScriptRoot
if (-not $Dest) {
    if ($Desktop) {
        $Dest = Join-Path ([Environment]::GetFolderPath("Desktop")) "T_Dubber_Kaggle_Pack"
    } else {
        $Dest = $RepoRoot
    }
}

# The exact set the Dockerfile promises. A missing one is a hard failure --
# a "successful" export with four binaries is worse than a red build, because
# it is discovered on Kaggle, not here.
$Expected = @("tgup", "stitcher", "normalizer", "subtitle_forge", "havaldar_core")

function Write-Step([string]$Message) { Write-Host "==> $Message" -ForegroundColor Cyan }
function Write-Ok([string]$Message)   { Write-Host "    $Message" -ForegroundColor Green }
function Stop-Pack([string]$Message)  { Write-Host "`nPACK FAILED: $Message" -ForegroundColor Red; exit 1 }

# ---------------------------------------------------------------- preflight
Write-Step "Preflight: Docker"
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Stop-Pack "docker CLI not on PATH. Install Docker Desktop (or add docker.exe to PATH)."
}
docker version --format "{{.Server.Version}}" *> $null
if ($LASTEXITCODE -ne 0) {
    Stop-Pack "Docker daemon is not running. Start Docker Desktop and retry."
}
# BuildKit's `--output type=local` needs the buildx plugin. Absent only on
# ancient installs -- detect explicitly rather than fail obscurely mid-build.
$UseBuildKit = $true
docker buildx version *> $null
if ($LASTEXITCODE -ne 0) { $UseBuildKit = $false }
Write-Ok "Docker OK (BuildKit: $UseBuildKit)"

# ---------------------------------------------------------------- build
$Staging = Join-Path ([System.IO.Path]::GetTempPath()) ("tdubber_pack_" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $Staging -Force | Out-Null

Write-Step "docker build --target pack-exporter (Go + Rust + C++ stages only)"
Push-Location $RepoRoot
$cid = $null
try {
    if ($UseBuildKit) {
        $BuildArgs = @("build", "--target", "pack-exporter", "-t", $Image)
        if ($NoCache) { $BuildArgs += "--no-cache" }
        $BuildArgs += @("--output", "type=local,dest=$Staging", ".")
        docker @BuildArgs
        if ($LASTEXITCODE -ne 0) { Stop-Pack "docker build failed (output above)." }
        $Exported = Join-Path $Staging "pack"
    } else {
        Write-Step "Buildx unavailable -- falling back to docker create + docker cp"
        $BuildArgs = @("build", "--target", "pack-exporter", "-t", $Image)
        if ($NoCache) { $BuildArgs += "--no-cache" }
        $BuildArgs += "."
        docker @BuildArgs
        if ($LASTEXITCODE -ne 0) { Stop-Pack "docker build failed (output above)." }
        # pack-exporter is `FROM scratch`: no shell, no CMD -- so the create
        # needs an explicit command. /pack/bin/tgup exists in the image; it is
        # never RUN, only used as the handle `docker cp` copies from.
        $cid = (docker create $Image /pack/bin/tgup 2>$null | Select-Object -Last 1)
        if (-not $cid) { Stop-Pack "docker create failed." }
        docker cp "${cid}:/pack" $Staging
        if ($LASTEXITCODE -ne 0) { Stop-Pack "docker cp /pack failed." }
        docker rm $cid *> $null
        $cid = $null
        $Exported = Join-Path $Staging "pack"
    }

    if (-not (Test-Path (Join-Path $Exported "bin"))) {
        Stop-Pack "export produced no pack\bin folder (got: $Exported)"
    }

    # -------------------------------------------------------------- verify
    Write-Step "Verifying export: presence, size, ELF magic, sha256"
    $SumPath = Join-Path $Exported "SHA256SUMS"
    if (-not (Test-Path $SumPath)) { Stop-Pack "SHA256SUMS missing from the export." }

    $Sums = @{}
    foreach ($Line in Get-Content $SumPath) {
        if ($Line -match '^([0-9a-f]{64})\s+(\S+)$') { $Sums[$Matches[2]] = $Matches[1] }
    }

    foreach ($Name in $Expected) {
        $Path = Join-Path (Join-Path $Exported "bin") $Name
        if (-not (Test-Path $Path)) { Stop-Pack "missing binary: $Name" }

        $Len = (Get-Item $Path).Length
        if ($Len -le 0) { Stop-Pack "$Name is empty (0 bytes)." }

        # ELF magic 7F 45 4C 46. Catches the one mistake that would otherwise
        # surface only on Kaggle: a Windows PE or a stub shipped in place of
        # the Linux binary.
        $Fs = [System.IO.File]::OpenRead($Path)
        try {
            $Buf = New-Object byte[] 4
            $N = $Fs.Read($Buf, 0, 4)
        } finally { $Fs.Close() }
        if ($N -ne 4 -or $Buf[0] -ne 0x7F -or $Buf[1] -ne 0x45 -or $Buf[2] -ne 0x4C -or $Buf[3] -ne 0x46) {
            Stop-Pack "$Name is not an ELF binary (wrong architecture or truncated export)."
        }

        $Hash = (Get-FileHash -Path $Path -Algorithm SHA256).Hash.ToLower()
        if (-not $Sums.ContainsKey($Name)) { Stop-Pack "$Name absent from SHA256SUMS." }
        if ($Sums[$Name] -ne $Hash) { Stop-Pack "$Name sha256 mismatch -- export is corrupt." }

        $Mb = [math]::Round($Len / 1MB, 1)
        Write-Host ("    {0,-16} {1,7} MB  {2}" -f $Name, $Mb, $Hash.Substring(0, 12)) -ForegroundColor Green
    }
    Write-Ok "5/5 binaries verified (ELF magic + sha256 match)"

    # -------------------------------------------------------------- land it
    $FinalRoot = Join-Path $Dest "pack"
    if (Test-Path $FinalRoot) {
        Write-Step "Replacing previous export at $FinalRoot"
        Remove-Item $FinalRoot -Recurse -Force
    }
    New-Item -ItemType Directory -Path $Dest -Force | Out-Null
    Copy-Item -Path $Exported -Destination $FinalRoot -Recurse
    Write-Ok "Exported: $FinalRoot"

    # -------------------------------------------------------------- summary
    $Total = 0
    foreach ($Name in $Expected) {
        $Total += (Get-Item (Join-Path (Join-Path $FinalRoot "bin") $Name)).Length
    }
    Write-Host ""
    Write-Host ("PACK READY -- {0} binaries, {1} MB total" -f $Expected.Count, [math]::Round($Total / 1MB, 1)) -ForegroundColor Green
    Write-Host "  folder : $FinalRoot"
    Write-Host "  image  : $Image (intermediate; safe to delete)"
    Write-Host ""
    Write-Host "Next steps -- one-time upload, then a 3-second Kaggle start:" -ForegroundColor Cyan
    Write-Host '  1. kaggle datasets create -p "<folder>\pack"' -ForegroundColor DarkGray
    Write-Host '     (needs %USERPROFILE%\.kaggle\kaggle.json; 3 top-level entries,' -ForegroundColor DarkGray
    Write-Host "      well under Kaggle's 50-file top-level limit)" -ForegroundColor DarkGray
    Write-Host '  2. Attach that dataset to kaggle_worker.ipynb. Cell 0 extracts it' -ForegroundColor DarkGray
    Write-Host '     to /kaggle/working/tdubber_pack, prepends bin/ to PATH and pins' -ForegroundColor DarkGray
    Write-Host '     NORMALIZER_BIN / STITCHER_BIN / TGUP_BIN by absolute path.' -ForegroundColor DarkGray
    Write-Host '     (PATH alone is not enough: Kaggle ships a foreign normalizer' -ForegroundColor DarkGray
    Write-Host '      that shadows the pack and forces the slow Python engine.)' -ForegroundColor DarkGray
    Write-Host '  3. tgup / stitcher / normalizer / subtitle_forge / havaldar_core are' -ForegroundColor DarkGray
    Write-Host '     then pinned for the whole session. No toolchain, no compile.' -ForegroundColor DarkGray
}
finally {
    Pop-Location
    if ($cid) { docker rm $cid *> $null 2>$null }
    if ($KeepStaging) {
        Write-Host "Staging kept at $Staging" -ForegroundColor DarkYellow
    } else {
        Remove-Item $Staging -Recurse -Force -ErrorAction SilentlyContinue
    }
}
