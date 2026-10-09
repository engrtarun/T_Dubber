[CmdletBinding()]
param(
    [string]$Name = "TARUN",
    [string]$Message
)

$ErrorActionPreference = "Stop"

if ($Name -notmatch '^[A-Za-z0-9_-]+$') {
    throw "Name must contain only letters, numbers, underscores, or hyphens."
}

if ([string]::IsNullOrWhiteSpace($Message)) {
    Write-Host "Apna message likho. Khatam karne ke liye blank line dabao."
    $lines = [System.Collections.Generic.List[string]]::new()
    while ($true) {
        $line = Read-Host
        if ([string]::IsNullOrEmpty($line)) {
            break
        }
        $lines.Add($line)
    }
    $Message = $lines -join "`n"
}

if ([string]::IsNullOrWhiteSpace($Message)) {
    throw "Message khaali hai; WEMD file nahi badli."
}

$filePath = Join-Path $PSScriptRoot "WE_ARE_TEAM.MD"
if (-not (Test-Path -LiteralPath $filePath -PathType Leaf)) {
    throw "WEMD file nahi mili: $filePath"
}

$newline = "`n"
$content = [System.IO.File]::ReadAllText($filePath)
if (-not $content.Contains("## TEAM CHAT - chronological")) {
    throw "Chronological TEAM CHAT section nahi mili; WEMD file nahi badli."
}

if ($content.Contains("`r`n")) {
    $newline = "`r`n"
}

$timestamp = Get-Date -Format "yyyy-MM-dd HH:mm"
$quotedMessage = ($Message -split '\r?\n' | ForEach-Object { "> $_" }) -join $newline
$entry = "### [$Name] $timestamp" + $newline + $quotedMessage + $newline + $newline

if ($content.Length -gt 0 -and -not $content.EndsWith("`n")) {
    $entry = $newline + $newline + $entry
}
elseif ($content.Length -gt 0) {
    $entry = $newline + $entry
}

$encoding = [System.Text.UTF8Encoding]::new($false)
$bytes = $encoding.GetBytes($entry)
$stream = [System.IO.File]::Open(
    $filePath,
    [System.IO.FileMode]::Append,
    [System.IO.FileAccess]::Write,
    [System.IO.FileShare]::Read
)
try {
    $stream.Write($bytes, 0, $bytes.Length)
}
finally {
    $stream.Dispose()
}

Write-Host "Message WEMD ke end me save ho gaya: $filePath"
Write-Host "Shared working copy me file padhne wale agents ise dekh sakte hain."
Write-Host "Alag clone/remote tak bhejne ke liye khud commit aur push karna hoga."
