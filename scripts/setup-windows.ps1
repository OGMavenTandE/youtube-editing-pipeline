# Create a venv for ytp on Scott's PC. Does not change system settings
# or touch any Python install outside the venv path.
param(
    [string]$Venv = "C:\AI Eval\YouTube\ytp\.venv",
    [string]$Repo = ""
)

$ErrorActionPreference = "Stop"
if (-not $Repo) {
    $Repo = Split-Path -Parent $PSScriptRoot
}

$spec = $null
foreach ($candidate in @("-3.12", "-3.11")) {
    & py $candidate -c "import sys; raise SystemExit(0 if sys.version_info[:2] in ((3, 12), (3, 11)) else 1)"
    if ($LASTEXITCODE -eq 0) {
        $spec = $candidate
        break
    }
}
if (-not $spec) {
    throw "Python 3.11 or 3.12 was not found via the py launcher. Install with: winget install Python.Python.3.12"
}

Write-Host "creating venv with py $spec at $Venv"
& py $spec -m venv $Venv
$python = Join-Path $Venv "Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "venv python was not created: $python"
}

& $python -m pip install --upgrade pip
# faster-whisper pins ctranslate2>=4,<5. Those CUDA 12 wheels need cuBLAS 12 and cuDNN 9.
& $python -m pip install -e ($Repo + "[whisper]")
& $python -m pip install "nvidia-cublas-cu12>=12.4,<13" "nvidia-cudnn-cu12>=9.1,<10"
& $python -c "import importlib.metadata as m; print('ctranslate2', m.version('ctranslate2')); print('cublas', m.version('nvidia-cublas-cu12')); print('cudnn', m.version('nvidia-cudnn-cu12'))"

Write-Host "self-test"
& $python -m ytp self-test
if ($LASTEXITCODE -ne 0) {
    throw "self-test failed"
}
