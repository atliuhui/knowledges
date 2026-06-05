param(
    [string]$Version = "0.38.2",
    [string]$Url,
    [string]$BinDir,
    [switch]$Force
)

# Download the `pocketbase` single-binary backend (static file server + SQLite
# + admin UI + JS hooks) from its GitHub Releases page and drop it into the
# repository-local `vendor\` directory so it travels alongside the mcp_server
# runtime, the `.venv` and the `models\` tree (same "everything next to the
# code" convention used by download-sense-voice.ps1).
#
# Usage:
#   .\actions\download-pocketbase.ps1                  # default version
#   .\actions\download-pocketbase.ps1 -Version 0.38.2
#   .\actions\download-pocketbase.ps1 -Force           # redownload
#   .\actions\download-pocketbase.ps1 -Url "https://.../pocketbase_x.y.z_windows_amd64.zip"
#
# After running, the binary is at <repo>\vendor\pocketbase.exe.

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $BinDir) {
    $BinDir = Join-Path $repoRoot "vendor"
}
$targetExe = Join-Path $BinDir "pocketbase.exe"

if ((Test-Path $targetExe) -and -not $Force) {
    Write-Host "==> pocketbase already exists at $targetExe (use -Force to redownload)"
    Get-Item $targetExe | Select-Object Name, Length, LastWriteTime
    return
}

if (-not $Url) {
    # Official Windows amd64 build from the pocketbase/pocketbase releases.
    $Url = "https://github.com/pocketbase/pocketbase/releases/download/v$Version/pocketbase_${Version}_windows_amd64.zip"
}

New-Item -ItemType Directory -Force -Path $BinDir | Out-Null

$tmpZip = Join-Path $BinDir "pocketbase-download.zip"
$tmpDir = Join-Path $BinDir "pocketbase-extract"
if (Test-Path $tmpZip) { Remove-Item $tmpZip -Force }
if (Test-Path $tmpDir) { Remove-Item $tmpDir -Recurse -Force }

Write-Host "==> Downloading $Url"
$curl = Get-Command curl.exe -ErrorAction SilentlyContinue
if ($curl) {
    & $curl.Source -L --fail --retry 3 -o $tmpZip $Url
    if ($LASTEXITCODE -ne 0) { throw "curl.exe failed with exit code $LASTEXITCODE" }
} else {
    $prevProgress = $ProgressPreference
    $ProgressPreference = 'SilentlyContinue'
    try {
        Invoke-WebRequest $Url -OutFile $tmpZip
    } finally {
        $ProgressPreference = $prevProgress
    }
}

if (-not (Test-Path $tmpZip)) {
    throw "Download did not produce $tmpZip"
}

Write-Host "==> Extracting"
Expand-Archive -Path $tmpZip -DestinationPath $tmpDir -Force

$extractedExe = Get-ChildItem -Path $tmpDir -Filter "pocketbase.exe" -Recurse |
    Select-Object -First 1
if (-not $extractedExe) {
    throw "pocketbase.exe not found inside $tmpZip"
}

if (Test-Path $targetExe) { Remove-Item $targetExe -Force }
Move-Item -Path $extractedExe.FullName -Destination $targetExe -Force

Remove-Item $tmpZip -Force -ErrorAction SilentlyContinue
Remove-Item $tmpDir -Recurse -Force -ErrorAction SilentlyContinue

# Smoke test: confirm the binary runs and report its version.
try {
    $ver = & $targetExe --version 2>&1
    Write-Host "==> pocketbase ready: $ver"
} catch {
    Write-Warning "pocketbase downloaded but '--version' failed: $_"
}

Get-Item $targetExe | Select-Object Name, Length, LastWriteTime
