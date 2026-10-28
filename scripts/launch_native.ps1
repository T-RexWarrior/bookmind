param([switch]$NoBrowser, [switch]$Rebuild)
$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $PSScriptRoot
$runtimeDir = Join-Path $projectDir 'data'
$webDir = Join-Path $projectDir 'web'
$pythonExe = Join-Path $projectDir '.venv\Scripts\python.exe'
New-Item -ItemType Directory -Force -Path $runtimeDir | Out-Null
Set-Location -LiteralPath $projectDir
$env:NEXT_TELEMETRY_DISABLED = '1'
if (-not (Test-Path -LiteralPath $pythonExe)) {
    $basePython = 'D:\miniconda3\python.exe'
    if (-not (Test-Path -LiteralPath $basePython)) { $basePython = 'python' }
    & $basePython -m venv (Join-Path $projectDir '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Cannot create Python environment.' }
}
& $pythonExe -c "import fastapi, yaml, openai, pymupdf, aiosqlite"
if ($LASTEXITCODE -ne 0) {
    & $pythonExe -m pip install --cache-dir (Join-Path $projectDir '.cache\pip') -e .
    if ($LASTEXITCODE -ne 0) { throw 'Native DeepTutor dependency installation failed.' }
}
$initialized = Join-Path $runtimeDir '.native-initialized'
if (-not (Test-Path -LiteralPath $initialized)) {
    & $pythonExe scripts\setup_native.py
    if ($LASTEXITCODE -ne 0) { throw 'Runtime setup failed.' }
    $legacyDatabase = Join-Path (Split-Path -Parent $projectDir) 'BookMind-v2\data\studytutor.db'
    if (Test-Path -LiteralPath $legacyDatabase) {
        & $pythonExe scripts\migrate_v2.py
        if ($LASTEXITCODE -ne 0) { throw 'Migration failed; the old application has not been changed.' }
    }
    'ready' | Set-Content -LiteralPath $initialized
}
if ($Rebuild -or -not (Test-Path -LiteralPath (Join-Path $webDir '.next\BUILD_ID'))) {
    Push-Location -LiteralPath $webDir
    try {
        if (-not (Test-Path -LiteralPath 'node_modules')) {
            & npm.cmd ci --cache (Join-Path $projectDir '.cache\npm') --no-audit --no-fund
            if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency installation failed.' }
        }
        & npm.cmd run build
        if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
    } finally { Pop-Location }
}
function Get-OwnedListener([int]$Port) {
    $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $listener) { return $null }
    $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener.OwningProcess)"
    if (-not $processInfo -or -not $processInfo.CommandLine.Contains($projectDir)) {
        throw "Port $Port belongs to another project. Stop that project first."
    }
    return $listener.OwningProcess
}
if (-not (Get-OwnedListener 8019)) {
    Start-Process -FilePath $pythonExe -ArgumentList @('-m','uvicorn','deeptutor.bookmind.app:app','--host','127.0.0.1','--port','8019','--no-access-log') -WorkingDirectory $projectDir -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runtimeDir 'backend.log') -RedirectStandardError (Join-Path $runtimeDir 'backend-error.log') | Out-Null
}
$ready = $false
for ($attempt = 0; $attempt -lt 120; $attempt++) {
    try { $health = Invoke-RestMethod 'http://127.0.0.1:8019/health/ready' -TimeoutSec 2; if ($health.status -eq 'ready') { $ready = $true; break } } catch {}
    Start-Sleep -Milliseconds 500
}
if (-not $ready) { throw 'Backend startup failed. See data/backend-error.log.' }
if (-not (Get-OwnedListener 8018)) {
    $nodeExe = (Get-Command node.exe).Source
    $nextBin = Join-Path $webDir 'node_modules\next\dist\bin\next'
    Start-Process -FilePath $nodeExe -ArgumentList @(('"' + $nextBin + '"'),'start','--hostname','127.0.0.1','--port','8018') -WorkingDirectory $webDir -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runtimeDir 'frontend.log') -RedirectStandardError (Join-Path $runtimeDir 'frontend-error.log') | Out-Null
}
$frontendReady = $false
for ($attempt = 0; $attempt -lt 40; $attempt++) {
    try { $page = Invoke-WebRequest 'http://127.0.0.1:8018/' -UseBasicParsing -TimeoutSec 2; if ($page.StatusCode -eq 200) { $frontendReady = $true; break } } catch {}
    Start-Sleep -Milliseconds 500
}
if (-not $frontendReady) { throw 'Frontend startup failed. See data/frontend-error.log.' }
@{ backend = (Get-OwnedListener 8019); frontend = (Get-OwnedListener 8018) } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $runtimeDir 'runtime-processes.json')
Write-Host 'BookMind is ready: http://127.0.0.1:8018' -ForegroundColor Green
if (-not $NoBrowser) { Start-Process 'http://127.0.0.1:8018' }
