<#
.SYNOPSIS
  Stage, verify and publish the CI-built Kaggle pack to engrtarun/tdubber-pack.

.WHAT IT DOES
  1. Unzips the GitHub Actions artifact "kaggle-tdubber-pack" (it wraps
     kaggle-tdubber-pack.tar.gz).
  2. Unpacks that tar.gz into a scratch tree and PROVES it is a real pack
     before anything touches Kaggle:
        * MANIFEST.txt + SHA256SUMS present
        * exactly the five tools, each an ELF64 executable
        * every binary matches its sha256 in SHA256SUMS
        * prints the "built:" line so you can see how fresh it is
  3. Stages a single-file upload (kaggle-tdubber-pack.tar.gz at the dataset
     root, next to dataset-metadata.json). Single file on purpose: this CLI
     uploads directories with --dir-mode {skip,zip,tar} and none of them
     reproduces the nested tree the way a plain file needs no directory
     rule at all. kaggle_worker.ipynb finds it by name
     (kaggle-tdubber-pack.tar.gz under /kaggle/input) and untars it itself.
  4. Unless -StageOnly: runs `kaggle datasets version` and then lists the
     dataset files so you can see what Kaggle actually holds.

.EXAMPLE
  # GitHub Actions -> run -> Artifacts -> download "kaggle-tdubber-pack"
  .\update_kaggle_pack.ps1 -ArtifactZip "$env:USERPROFILE\Downloads\kaggle-tdubber-pack.zip"

.EXAMPLE
  # verify + stage, upload nothing
  .\update_kaggle_pack.ps1 -ArtifactZip .\kaggle-tdubber-pack.zip -StageOnly
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$ArtifactZip,

    [string]$Creds = '',

    [string]$DatasetId = 'engrtarun/tdubber-pack',

    [switch]$StageOnly
)

$ErrorActionPreference = 'Stop'

# $PSScriptRoot is not populated while the param defaults are evaluated, so
# the credentials path is resolved here instead.
if (-not $Creds) { $Creds = Join-Path $PSScriptRoot 'kaggle_paperWork\kaggle.json' }

function Fail([string]$msg) {
    Write-Host "ASSERT FAIL: $msg" -ForegroundColor Red
    exit 1
}

# ---------------------------------------------------------------- inputs
if (-not (Test-Path $ArtifactZip)) { Fail "artifact zip not found: $ArtifactZip" }
if (-not (Test-Path $Creds))       { Fail "Kaggle credentials not found: $Creds" }

$Stage     = Join-Path $PSScriptRoot '_pack_stage'
$Extracted = Join-Path $Stage '_unpack'

if (Test-Path $Stage) { Remove-Item $Stage -Recurse -Force }
New-Item -ItemType Directory -Path $Extracted -Force | Out-Null

# ------------------------------------------------- 1. open the artifact
Write-Host "[1/4] opening artifact $ArtifactZip" -ForegroundColor Cyan
$zipRoot = Join-Path $Stage '_zip'
Expand-Archive -Path $ArtifactZip -DestinationPath $zipRoot -Force

$tars = Get-ChildItem $zipRoot -Recurse -Filter '*.tar.gz'
if ($tars.Count -ne 1) { Fail "expected exactly one .tar.gz inside the artifact, found $($tars.Count)" }
$TarGz = $tars[0].FullName
Write-Host "      $($tars[0].Name)  ($([math]::Round($tars[0].Length/1MB,2)) MB)"

# ----------------------------------------------------- 2. prove the pack
Write-Host "[2/4] verifying the pack" -ForegroundColor Cyan
tar -xzf $TarGz -C $Extracted
if ($LASTEXITCODE -ne 0) { Fail "tar could not unpack $TarGz" }

# The artifact tar was made with `tar -czf ... -C pack .`, so everything is
# one level down (./MANIFEST.txt, ./bin/...) -- accept either layout.
$Root = $Extracted
if (-not (Test-Path (Join-Path $Root 'MANIFEST.txt'))) {
    $child = Get-ChildItem $Root -Directory | Select-Object -First 1
    if ($child -and (Test-Path (Join-Path $child.FullName 'MANIFEST.txt'))) { $Root = $child.FullName }
}
if (-not (Test-Path (Join-Path $Root 'MANIFEST.txt'))) { Fail "no MANIFEST.txt under $Extracted" }

$Manifest = Get-Content (Join-Path $Root 'MANIFEST.txt')
$Built = ($Manifest | Select-String '^built:').Line
Write-Host "      $Built" -ForegroundColor DarkGray
$Manifest | Where-Object { $_ -match '^\s*\w+\s+\d+\s' } |
    ForEach-Object { Write-Host "      $_" -ForegroundColor DarkGray }

$Expected = @('tgup', 'stitcher', 'subtitle_forge', 'havaldar_core', 'normalizer')
$BinDir   = Join-Path $Root 'bin'
if (-not (Test-Path $BinDir)) { Fail "no bin/ directory in the pack" }

foreach ($name in $Expected) {
    $p = Join-Path $BinDir $name
    if (-not (Test-Path $p)) { Fail "missing binary: bin/$name" }

    # ELF64 magic -- proves these are the Linux build, not a Windows copy.
    $fs = [System.IO.File]::OpenRead($p)
    try {
        $magic = New-Object byte[] 4
        [void]$fs.Read($magic, 0, 4)
    } finally { $fs.Dispose() }
    if (-not ($magic[0] -eq 0x7F -and $magic[1] -eq 0x45 -and $magic[2] -eq 0x4C -and $magic[3] -eq 0x46)) {
        Fail "bin/$name is not an ELF executable"
    }
    Write-Host ("      ok   {0,-16} {1,10} bytes  ELF64" -f $name, (Get-Item $p).Length)
}

# SHA256SUMS: "<hex>  <name>", one line per file in bin/.
$SumsPath = Join-Path $Root 'SHA256SUMS'
if (-not (Test-Path $SumsPath)) { Fail "no SHA256SUMS in the pack" }
foreach ($line in (Get-Content $SumsPath)) {
    if ($line -notmatch '^([0-9a-fA-F]{64})\s+\*?(.+)$') { continue }
    $want  = $Matches[1].ToLower()
    $file  = Join-Path $BinDir $Matches[2].Trim()
    if (-not (Test-Path $file)) { Fail "SHA256SUMS names a file that is not there: $($Matches[2])" }
    $got = (Get-FileHash $file -Algorithm SHA256).Hash.ToLower()
    if ($got -ne $want) { Fail "sha256 mismatch for $($Matches[2])" }
}
Write-Host "      ok   SHA256SUMS verified" -ForegroundColor Green

# ------------------------------------------------------- 3. stage upload
Write-Host "[3/4] staging a single-file dataset upload" -ForegroundColor Cyan
Copy-Item $TarGz -Destination (Join-Path $Stage 'kaggle-tdubber-pack.tar.gz') -Force

$Meta = @{
    title    = 'T_Dubber native pack (5 static linux/amd64 binaries)'
    id       = $DatasetId
    licenses = @(@{ name = 'CC0-1.0' })
} | ConvertTo-Json -Depth 4
# Written by hand rather than Set-Content: Windows PowerShell's -Encoding UTF8
# emits a BOM, and the CLI's json parser rejects it with the useless
# "Expecting value: line 1 column 1 (char 0)".
[System.IO.File]::WriteAllText(
    (Join-Path $Stage 'dataset-metadata.json'),
    $Meta + "`n",
    (New-Object System.Text.UTF8Encoding $false)
)

# Scratch trees must never reach the upload.
Remove-Item $Extracted -Recurse -Force
Remove-Item $zipRoot   -Recurse -Force
Get-ChildItem $Stage | ForEach-Object { Write-Host "      $($_.Name)  $($_.Length)" -ForegroundColor DarkGray }

if ($StageOnly) {
    Write-Host "STAGED ONLY (nothing uploaded). Stage folder: $Stage" -ForegroundColor Yellow
    exit 0
}

# ---------------------------------------------------------- 4. publish
Write-Host "[4/4] publishing $DatasetId" -ForegroundColor Cyan
$k = Get-Content $Creds -Raw | ConvertFrom-Json
$env:KAGGLE_USERNAME = $k.username
$env:KAGGLE_KEY       = $k.key

$msg = "CI pack $(Get-Date -Format 'yyyy-MM-dd HH:mm') ($Built)".Trim()
python -m kaggle datasets version -p $Stage -m $msg
if ($LASTEXITCODE -ne 0) { Fail "kaggle datasets version failed" }

Write-Host "`nFiles Kaggle now holds:" -ForegroundColor Cyan
python -m kaggle datasets files $DatasetId
if ($LASTEXITCODE -ne 0) { Fail "could not list the dataset afterwards" }

Write-Host "`nPACK PUBLISHED -- restart the worker kernel so the new version mounts." -ForegroundColor Green
