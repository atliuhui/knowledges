param(
    [int]$Port,
    [string]$Interface,
    [string]$Root,
    [string]$DataDir,
    [string]$Binary
)

# Start PocketBase to serve the whole knowledge-base root over loopback HTTP.
# Agent-generated H5 offline apps live under
# %USERPROFILE%\knowledges\apps\<slug>\ and are reachable at
# http://127.0.0.1:<port>/apps/<slug>/ once this script is running.
#
# PocketBase serves every file under `--publicDir` as static content at `/`,
# so the existing apps\<slug>\index.html layout is preserved. The admin UI
# (and JS hooks / collections, if you choose to use them later) lives under
# `/_/` and the REST API under `/api/`; both prefixes are reserved by
# PocketBase and never collide with `apps\`.
#
# Defaults are read from config.yaml -> knowledge_base_root + paths + apps:
#   knowledge_base_root: "%USERPROFILE%\\knowledges"
#   paths:
#     apps_data_dir: "apps_data"   # local web service $DataDir, sibling of apps\
#   apps:
#     host: "127.0.0.1"
#     port: 8788
#
# Override via CLI:
#   .\actions\start-pocketbase.ps1
#   .\actions\start-pocketbase.ps1 -Port 9000
#   .\actions\start-pocketbase.ps1 -Interface 0.0.0.0   # exposes on LAN
#   .\actions\start-pocketbase.ps1 -Root "D:\shared\knowledges"
#   .\actions\start-pocketbase.ps1 -DataDir "D:\shared\apps_data"

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot

if (-not $Binary) {
    $Binary = Join-Path $repoRoot "vendor\pocketbase.exe"
}
if (-not (Test-Path $Binary)) {
    $cmd = Get-Command pocketbase -ErrorAction SilentlyContinue
    if ($cmd) {
        $Binary = $cmd.Source
    } else {
        throw "pocketbase not found. Run .\actions\download-pocketbase.ps1 first, or pass -Binary <path>."
    }
}

# Resolve config.yaml defaults via Python (avoids a duplicate YAML parser in PS).
$pythonExe = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $pythonExe)) { $pythonExe = "python" }

$cfgJson = & $pythonExe -c @"
import json
from services.config import load_config
cfg = load_config()
print(json.dumps({
    'root': str(cfg.knowledge_base_root),
    'host': cfg.apps.host,
    'port': cfg.apps.port,
    'apps_data': str(cfg.apps_data_dir),
}))
"@
if ($LASTEXITCODE -ne 0) { throw "Failed to read apps config via python: $cfgJson" }
$cfg = $cfgJson | ConvertFrom-Json

if (-not $Root)      { $Root      = $cfg.root }
if (-not $Interface) { $Interface = $cfg.host }
if (-not $Port)      { $Port      = [int]$cfg.port }
if (-not $DataDir)   { $DataDir   = $cfg.apps_data }

New-Item -ItemType Directory -Force -Path $Root    | Out-Null
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null

$bind = "${Interface}:${Port}"

# Pick a friendlier host for the printed URLs when binding to a wildcard.
if ($Interface -in @('0.0.0.0', '::', '*', '')) {
    $displayHost = 'localhost'
} else {
    $displayHost = $Interface
}
$displayBind = "${displayHost}:${Port}"

Write-Host "==> Serving $Root"
Write-Host "    binding   http://$bind/"
Write-Host "    apps URL  http://$displayBind/apps/<slug>/"
Write-Host "    admin UI  http://$displayBind/_/"
if ($Interface -in @('0.0.0.0', '::', '*', '')) {
    $lanIps = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' -and $_.PrefixOrigin -ne 'WellKnown' } |
        Select-Object -ExpandProperty IPAddress
    foreach ($ip in $lanIps) {
        Write-Host "    LAN URL   http://${ip}:${Port}/apps/<slug>/"
    }
}
Write-Host "    apps_data $DataDir"
Write-Host "    binary    $Binary"
Write-Host "    (Ctrl+C to stop)"

& $Binary serve `
    --http $bind `
    --dir $DataDir `
    --publicDir $Root
