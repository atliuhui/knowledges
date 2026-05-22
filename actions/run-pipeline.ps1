param(
    [switch]$Force,
    [string[]]$Only = @(),
    [switch]$NoVector,
    [string]$Config,
    [string]$KbRoot
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$pythonExe = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $pythonExe)) {
    throw "Python not found in .venv: $pythonExe"
}

function Invoke-Tool {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Tool,
        [string[]]$ExtraFlags = @()
    )

    $commandParts = @("-m", "tools.$Tool")
    if ($Config) {
        $commandParts += @("--config", $Config)
    }
    if ($KbRoot) {
        $commandParts += @("--kb-root", $KbRoot)
    }
    $commandParts += $ExtraFlags

    Write-Host "==> Running: $pythonExe $($commandParts -join ' ')"
    & $pythonExe @commandParts
    $exitCode = $LASTEXITCODE
    Write-Host "<== Exit: $Tool => $exitCode"
    return $exitCode
}

$onlyFlags = @()
foreach ($id in $Only) {
    if ($id) {
        $onlyFlags += @("--only", $id)
    }
}

$scanCode = Invoke-Tool -Tool "scan"
$convertFlags = @()
if ($Force) {
    $convertFlags += "--force"
}
$convertFlags += $onlyFlags
$convertCode = Invoke-Tool -Tool "convert" -ExtraFlags $convertFlags

$ingestFlags = @()
if ($Force) {
    $ingestFlags += "--force"
}
if ($NoVector) {
    $ingestFlags += "--no-vector"
}
$ingestFlags += $onlyFlags
$ingestCode = Invoke-Tool -Tool "ingest" -ExtraFlags $ingestFlags

$finalCode = 0
if ($scanCode -ne 0) { $finalCode = $scanCode }
if ($convertCode -ne 0) { $finalCode = $convertCode }
if ($ingestCode -ne 0) { $finalCode = $ingestCode }

Write-Host "Pipeline summary: scan=$scanCode convert=$convertCode ingest=$ingestCode"
exit $finalCode
