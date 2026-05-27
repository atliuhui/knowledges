param(
    [int]$Port,
    [string]$Interface,
    [string]$Root,
    [string]$Binary
)

# Start miniserve to serve the whole knowledge-base root over loopback HTTP.
# Agent-generated H5 offline apps live under
# %USERPROFILE%\knowledges\apps\<slug>\ and are reachable at
# http://127.0.0.1:<port>/apps/<slug>/ once this script is running.
#
# Defaults are read from config.yaml -> knowledge_base_root + apps:
#   knowledge_base_root: "%USERPROFILE%\\knowledges"
#   apps:
#     host: "127.0.0.1"
#     port: 8788
#
# Override via CLI:
#   .\actions\start-apps-server.ps1
#   .\actions\start-apps-server.ps1 -Port 9000
#   .\actions\start-apps-server.ps1 -Interface 0.0.0.0   # exposes on LAN
#   .\actions\start-apps-server.ps1 -Root "D:\shared\apps"

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot

if (-not $Binary) {
    $Binary = Join-Path $repoRoot "vendor\miniserve.exe"
}
if (-not (Test-Path $Binary)) {
    $cmd = Get-Command miniserve -ErrorAction SilentlyContinue
    if ($cmd) {
        $Binary = $cmd.Source
    } else {
        throw "miniserve not found. Run .\actions\download-miniserve.ps1 first, or pass -Binary <path>."
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
}))
"@
if ($LASTEXITCODE -ne 0) { throw "Failed to read apps config via python: $cfgJson" }
$cfg = $cfgJson | ConvertFrom-Json

if (-not $Root)      { $Root      = $cfg.root }
if (-not $Interface) { $Interface = $cfg.host }
if (-not $Port)      { $Port      = [int]$cfg.port }

New-Item -ItemType Directory -Force -Path $Root | Out-Null

Write-Host "==> Serving $Root"
Write-Host "    binding   http://${Interface}:${Port}/"
Write-Host "    binary    $Binary"
Write-Host "    (Ctrl+C to stop)"

& $Binary `
    --interfaces $Interface `
    --port $Port `
    --index "index.html" `
    --hide-version-footer `
    --title "Knowledges Apps" `
    $Root
