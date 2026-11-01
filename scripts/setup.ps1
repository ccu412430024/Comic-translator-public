param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $taskRoot
& $Python -c 'import sys; assert sys.version_info[:2] == (3,12), "Python 3.12 is required"'
if ($LASTEXITCODE -ne 0) { throw 'Choose a Python 3.12 interpreter with -Python.' }
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    & $Python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed.' }
}
if (-not (Test-Path -LiteralPath 'vendor\comic-translate\modules')) {
    New-Item -ItemType Directory -Path 'vendor' -Force | Out-Null
    git clone https://github.com/ogkalu2/comic-translate.git vendor/comic-translate
    if ($LASTEXITCODE -ne 0) { throw 'Upstream clone failed.' }
    git -C vendor/comic-translate checkout 8977b91a4f7a40c3917c5a268e9e7d78e1d818da
    if ($LASTEXITCODE -ne 0) { throw 'Upstream checkout failed.' }
}
& '.\.venv\Scripts\python.exe' -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& '.\.venv\Scripts\python.exe' scripts/prepare_assets.py --include-base
if ($LASTEXITCODE -ne 0) { throw 'Model preparation failed.' }
Write-Output 'Ready. Run scripts/start-reader.ps1.'
