param(
    [string]$Config,
    [string]$KbRoot
)

# Test warmup ("test-warmup"):
# Runs the kb.warmup logic in a SEPARATE process from the MCP server, so the
# Python-level caches (jieba dict, Tantivy/LanceDB handles, OllamaEmbedder
# singleton) do NOT carry over to the actual server. Use this script to:
#   - observe per-stage startup cost / timings
#   - load the Ollama model into memory (keep_alive) before launching MCP server
#   - warm OS page cache for the index files
# For real in-process warmup, call the kb.warmup MCP tool from the agent, or
# add startup warmup to server.main().

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$pythonExe = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $pythonExe)) {
    throw "Python not found in .venv: $pythonExe"
}

# kb.warmup is only exposed as an MCP tool; invoke it in-process by
# initializing server._CFG and calling kb_warmup() directly.
#
# NOTE: this runs in a *separate* process from the actual MCP server, so the
# Python-level caches (jieba dict, Tantivy/LanceDB handles, OllamaEmbedder
# singleton) do NOT carry over. Useful mainly for:
#   - observing startup cost / per-stage timings
#   - keeping the Ollama model resident (keep_alive) and warming OS page cache
$pyScript = @'
import json, os, sys
from services.config import load_config
import server

config_path = os.environ.get('KB_WARMUP_CONFIG') or None
kb_root = os.environ.get('KB_WARMUP_KB_ROOT') or None
server._CFG = load_config(
    config_path=config_path,
    knowledge_base_root_override=kb_root,
)
server._CFG.ensure_dirs()
result = server.kb_warmup()
print(json.dumps(result, ensure_ascii=False, indent=2))
sys.exit(0 if result.get('ok') else 1)
'@

if ($Config) { $env:KB_WARMUP_CONFIG = $Config } else { Remove-Item Env:KB_WARMUP_CONFIG -ErrorAction SilentlyContinue }
if ($KbRoot) { $env:KB_WARMUP_KB_ROOT = $KbRoot } else { Remove-Item Env:KB_WARMUP_KB_ROOT -ErrorAction SilentlyContinue }

Write-Host "==> Warming up knowledge base (jieba / fulltext / vector / embedder)..."
& $pythonExe -c $pyScript
$exitCode = $LASTEXITCODE
Write-Host "<== Exit: warmup => $exitCode"
exit $exitCode
