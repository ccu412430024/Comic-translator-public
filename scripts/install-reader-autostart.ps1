param([switch]$BuildOnly, [switch]$Uninstall)
$ErrorActionPreference = 'Stop'
$comicRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$comicPython = Join-Path $comicRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $comicPython)) { throw 'Missing project .venv.' }
$comicArguments = @((Join-Path $PSScriptRoot 'install_reader_autostart.py'))
if ($BuildOnly) { $comicArguments += '--build-only' }
if ($Uninstall) { $comicArguments += '--uninstall' }
& $comicPython @comicArguments
if ($LASTEXITCODE -ne 0) { throw 'Automatic startup bridge installation failed.' }
