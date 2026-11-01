$ErrorActionPreference = 'Stop'
$comicRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$comicPython = Join-Path $comicRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $comicPython)) { throw 'Missing .venv. Install the project dependencies first.' }
$comicReady = $false
try {
    $comicHealth = Invoke-RestMethod 'http://127.0.0.1:8765/api/health' -TimeoutSec 2
    $comicReady = $comicHealth.app -eq 'comic-reader'
} catch {}
if (-not $comicReady) {
    $comicLogs = Join-Path $comicRoot '.runtime\reader'
    New-Item -ItemType Directory -Force -Path $comicLogs | Out-Null
    $comicScript = Join-Path $comicRoot 'scripts\serve_reader.py'
    $comicProcess = Start-Process -FilePath $comicPython -ArgumentList @('-u', ('"' + $comicScript + '"')) -WorkingDirectory $comicRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $comicLogs 'server.log') -RedirectStandardError (Join-Path $comicLogs 'server-error.log') -PassThru
    for ($comicAttempt = 0; $comicAttempt -lt 30; $comicAttempt++) {
        Start-Sleep -Milliseconds 500
        if ($comicProcess.HasExited) { throw 'Reader failed to start. See .runtime/reader/server-error.log.' }
        try {
            $comicHealth = Invoke-RestMethod 'http://127.0.0.1:8765/api/health' -TimeoutSec 1
            if ($comicHealth.app -eq 'comic-reader') { $comicReady = $true; break }
        } catch {}
    }
    if (-not $comicReady) { throw 'Reader did not become ready. See .runtime/reader/server-error.log.' }
}
Write-Host 'Comic reader: http://127.0.0.1:8765/'
Write-Host ('Chrome extension folder: ' + (Join-Path $comicRoot 'chrome-extension'))
