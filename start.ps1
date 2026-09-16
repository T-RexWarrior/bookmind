# 学迹 / BookMind — Windows single-machine startup (PRODUCTIZATION §13 fallback).
# No Docker / PostgreSQL needed. Starts the API (serving the frontend at /ui)
# with the official DeepSeek API when its key environment/file is configured.
#
# Usage:  powershell -ExecutionPolicy Bypass -File start.ps1
#         then open http://localhost:18765/ui

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

# Read host/port from environment with the same defaults as config.py.
$Host_ = if ($env:BOOKMIND_HOST) { $env:BOOKMIND_HOST } else { "127.0.0.1" }
$Port  = if ($env:BOOKMIND_PORT) { $env:BOOKMIND_PORT } else { "18765" }

# Detect if the port is already in use and give an actionable message.
$inUse = $false
try {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Any, [int]$Port)
    $listener.Start()
    $listener.Stop()
} catch {
    $inUse = $true
}

if ($inUse) {
    Write-Host "BookMind cannot start: port $Port is already in use." -ForegroundColor Yellow
    Write-Host "Choose another port, for example:" -ForegroundColor Yellow
    Write-Host "  `$env:BOOKMIND_PORT=18800; .\start.ps1" -ForegroundColor Cyan
    exit 1
}

$env:PYTHONPATH = "backend"
$env:PYTHONUTF8 = "1"

$ProjectPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $ProjectPython)) {
    Write-Host "BookMind runtime is not installed yet. Run:" -ForegroundColor Yellow
    Write-Host "  py -3.11 -m venv .venv" -ForegroundColor Cyan
    Write-Host "  .\.venv\Scripts\python.exe -m pip install -r requirements.txt" -ForegroundColor Cyan
    exit 1
}
& $ProjectPython -c "import uvicorn, bookmind" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "BookMind dependencies are incomplete. Run:" -ForegroundColor Yellow
    Write-Host "  .\.venv\Scripts\python.exe -m pip install -r requirements.txt" -ForegroundColor Cyan
    exit 1
}

Write-Host "BookMind is starting." -ForegroundColor Green
Write-Host "UI:    http://${Host_}:$Port/ui/"
Write-Host "API:   http://${Host_}:$Port/docs"
$envFileHasKey = $false
$envFileHasKeyFile = $false
if (Test-Path -LiteralPath ".env") {
    $envFileHasKey = [bool](Select-String -LiteralPath ".env" -Pattern '^DEEPSEEK_API_KEY=.+$' -Quiet)
    $keyFileLine = Select-String -LiteralPath ".env" -Pattern '^BOOKMIND_LLM_API_KEY_FILE=(.+)$' | Select-Object -First 1
    if ($keyFileLine) {
        $keyFilePath = $keyFileLine.Matches[0].Groups[1].Value.Trim()
        $envFileHasKeyFile = [bool]($keyFilePath -and (Test-Path -LiteralPath $keyFilePath))
    }
}
$keySet = if ($env:DEEPSEEK_API_KEY -or $envFileHasKey -or $envFileHasKeyFile) { "DeepSeek official API enabled" } else { "offline fallback" }
Write-Host "Model: $keySet"
Write-Host "Press Ctrl+C to stop."
Write-Host ""

& $ProjectPython -m uvicorn bookmind.api.app:app --host $Host_ --port $Port
