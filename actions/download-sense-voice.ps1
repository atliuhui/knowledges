param(
    [string]$Url = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2",
    [string]$ModelsDir,
    [switch]$Force
)

# Download and extract the SenseVoice-Small ONNX bundle for sherpa-onnx.
# The resulting directory will contain `model.int8.onnx` (~240 MB) and
# `tokens.txt` (~50 KB), and lives alongside the mcp_server runtime so that
# the model travels with the codebase rather than the user's home directory.

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $ModelsDir) {
    $ModelsDir = Join-Path $repoRoot "models"
}
$targetDir = Join-Path $ModelsDir "sense-voice"

if ((Test-Path $targetDir) -and -not $Force) {
    Write-Host "==> sense-voice already exists at $targetDir (use -Force to redownload)"
    Get-ChildItem $targetDir | Select-Object Name, Length
    return
}

if ((Test-Path $targetDir) -and $Force) {
    Write-Host "==> Removing existing $targetDir"
    Remove-Item -Recurse -Force $targetDir
}

New-Item -ItemType Directory -Force -Path $ModelsDir | Out-Null
$archive = Join-Path $ModelsDir "sense-voice.tar.bz2"

Write-Host "==> Downloading $Url"
Write-Host "    (archive is ~500-700 MB; it bundles both fp32 and int8 ONNX models)"
$curl = Get-Command curl.exe -ErrorAction SilentlyContinue
if ($curl) {
    # curl.exe is dramatically faster than Invoke-WebRequest on PS 5.1.
    & $curl.Source -L --fail --retry 3 -o $archive $Url
    if ($LASTEXITCODE -ne 0) { throw "curl.exe failed with exit code $LASTEXITCODE" }
} else {
    # Disabling the progress bar speeds IWR up by ~5-10x.
    $prevProgress = $ProgressPreference
    $ProgressPreference = 'SilentlyContinue'
    try {
        Invoke-WebRequest $Url -OutFile $archive
    } finally {
        $ProgressPreference = $prevProgress
    }
}

Write-Host "==> Extracting to $ModelsDir"
tar -xjf $archive -C $ModelsDir

$extracted = Join-Path $ModelsDir "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
if (-not (Test-Path $extracted)) {
    throw "Extraction did not produce expected directory: $extracted"
}
Rename-Item $extracted $targetDir

Remove-Item $archive -Force

# Trim files we don't use: keep only model.int8.onnx + tokens.txt (~240 MB total),
# drop the fp32 model.onnx (~900 MB) and sample test_wavs/.
$fp32 = Join-Path $targetDir "model.onnx"
if (Test-Path $fp32) {
    Write-Host "==> Removing unused fp32 model: $fp32"
    Remove-Item $fp32 -Force
}
$testWavs = Join-Path $targetDir "test_wavs"
if (Test-Path $testWavs) {
    Write-Host "==> Removing sample test_wavs: $testWavs"
    Remove-Item -Recurse -Force $testWavs
}

Write-Host "==> sense-voice ready at $targetDir"
Get-ChildItem $targetDir | Select-Object Name, Length
