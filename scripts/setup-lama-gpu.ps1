$ErrorActionPreference = 'Stop'
$comicRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$comicBasePython = Join-Path $comicRoot '.venv\Scripts\python.exe'
$comicGpuEnv = Join-Path $comicRoot '.venv-lama-gpu'
$comicGpuPython = Join-Path $comicGpuEnv 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $comicGpuPython)) {
    & $comicBasePython -m venv $comicGpuEnv
    if ($LASTEXITCODE) { throw 'Could not create isolated LaMa GPU environment.' }
}
& $comicGpuPython -m pip install -r (Join-Path $comicRoot 'requirements-lama-gpu.txt')
if ($LASTEXITCODE) { throw 'Could not install isolated LaMa DirectML runtime.' }
Write-Host 'LaMa DirectML runtime ready. Restart the reader to use it.'
