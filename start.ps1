param([switch]$NoBrowser, [switch]$Rebuild)
$ErrorActionPreference = 'Stop'
$projectDir = $PSScriptRoot
$pythonExe = Join-Path $projectDir '.venv\Scripts\python.exe'
$dataDir = Join-Path $projectDir 'data'
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
Set-Location -LiteralPath $projectDir
if (-not (Test-Path -LiteralPath $pythonExe)) {
    $basePython = 'D:\miniconda3\python.exe'
    if (-not (Test-Path -LiteralPath $basePython)) { $basePython = 'python' }
    & $basePython -m venv (Join-Path $projectDir '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Cannot create Python environment.' }
}
& $pythonExe -c "import importlib.util, sys; sys.exit(0 if all(importlib.util.find_spec(n) for n in ['fastapi','pymupdf','httpx','multipart','defusedxml','uvicorn']) else 1)"
if ($LASTEXITCODE -ne 0) {
    & $pythonExe -m pip install --cache-dir (Join-Path $projectDir '.cache\pip') -r backend\requirements.lock.txt
    if ($LASTEXITCODE -ne 0) { throw 'Python dependency install failed.' }
}
if ($Rebuild -or -not (Test-Path -LiteralPath (Join-Path $projectDir 'frontend\dist\index.html'))) {
    Push-Location (Join-Path $projectDir 'frontend')
    try {
        if (-not (Test-Path -LiteralPath 'node_modules')) {
            & npm.cmd ci --cache (Join-Path $projectDir '.cache\npm') --no-audit --no-fund
            if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency install failed.' }
        }
        & npm.cmd run build
        if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
    } finally { Pop-Location }
}
$url = 'http://127.0.0.1:8018'
$running = $false
try { $health = Invoke-RestMethod "$url/api/health" -TimeoutSec 2; $running = $health.app -eq 'bookmind' } catch {}
if (-not $running) {
    $listener = Get-NetTCPConnection -LocalPort 8018 -State Listen -ErrorAction SilentlyContinue
    if ($listener) { throw 'Port 8018 is already occupied by another application.' }
    $backendDir = Join-Path $projectDir 'backend'
    $server = Start-Process -FilePath $pythonExe -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--app-dir', ('"' + $backendDir + '"'), '--host', '127.0.0.1', '--port', '8018') -WorkingDirectory $projectDir -WindowStyle Hidden -RedirectStandardOutput (Join-Path $dataDir 'server.log') -RedirectStandardError (Join-Path $dataDir 'server-error.log') -PassThru
    $server.Id | Set-Content -LiteralPath (Join-Path $dataDir 'server.pid')
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Milliseconds 500
        try { $health = Invoke-RestMethod "$url/api/health" -TimeoutSec 2; if ($health.app -eq 'bookmind') { $running = $true; break } } catch {}
        if ($server.HasExited) { break }
    }
    if (-not $running) { throw 'Server failed to start. See data/server-error.log.' }
}
Write-Host "BookMind is ready: $url" -ForegroundColor Green
if (-not $NoBrowser) { Start-Process $url }
