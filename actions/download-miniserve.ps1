param(
    [string]$Version = "0.35.0",
    [string]$Url,
    [string]$BinDir,
    [switch]$Force
)

# Download the `miniserve` static file server (single binary, no install) from
# its GitHub Releases page and drop it into the repository-local `vendor\`
# directory so it travels alongside the mcp_server runtime, the `.venv` and
# the `models\` tree (same "everything next to the code" convention used by
# download-sense-voice.ps1).
#
# Usage:
#   .\actions\download-miniserve.ps1                  # default version
#   .\actions\download-miniserve.ps1 -Version 0.35.0
#   .\actions\download-miniserve.ps1 -Force           # redownload
#   .\actions\download-miniserve.ps1 -Url "https://.../miniserve-...exe"
#
# After running, the binary is at <repo>\vendor\miniserve.exe.

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $BinDir) {
    $BinDir = Join-Path $repoRoot "vendor"
}
$targetExe = Join-Path $BinDir "miniserve.exe"

if ((Test-Path $targetExe) -and -not $Force) {
    Write-Host "==> miniserve already exists at $targetExe (use -Force to redownload)"
    Get-Item $targetExe | Select-Object Name, Length, LastWriteTime
    return
}

if (-not $Url) {
    # Official Windows x86_64 MSVC build from the svenstaro/miniserve releases.
    $Url = "https://github.com/svenstaro/miniserve/releases/download/v$Version/miniserve-$Version-x86_64-pc-windows-msvc.exe"
}

New-Item -ItemType Directory -Force -Path $BinDir | Out-Null

Write-Host "==> Downloading $Url"
$curl = Get-Command curl.exe -ErrorAction SilentlyContinue
if ($curl) {
    & $curl.Source -L --fail --retry 3 -o $targetExe $Url
    if ($LASTEXITCODE -ne 0) { throw "curl.exe failed with exit code $LASTEXITCODE" }
} else {
    $prevProgress = $ProgressPreference
    $ProgressPreference = 'SilentlyContinue'
    try {
        Invoke-WebRequest $Url -OutFile $targetExe
    } finally {
        $ProgressPreference = $prevProgress
    }
}

if (-not (Test-Path $targetExe)) {
    throw "Download did not produce $targetExe"
}

# Smoke test: confirm the binary runs and report its version.
try {
    $ver = & $targetExe --version 2>&1
    Write-Host "==> miniserve ready: $ver"
} catch {
    Write-Warning "miniserve downloaded but '--version' failed: $_"
}

Get-Item $targetExe | Select-Object Name, Length, LastWriteTime
