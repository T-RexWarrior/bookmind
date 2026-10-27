$ErrorActionPreference = 'Stop'
$pidFile = Join-Path $PSScriptRoot 'data\server.pid'
if (-not (Test-Path -LiteralPath $pidFile)) { Write-Host 'No recorded server process.'; exit }
$serverId = [int](Get-Content -LiteralPath $pidFile)
$processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $serverId"
if ($processInfo -and $processInfo.CommandLine.Contains('app.main:app') -and $processInfo.CommandLine.Contains((Join-Path $PSScriptRoot 'backend'))) {
    Stop-Process -Id $serverId
    Write-Host 'BookMind stopped. Learning data is saved.'
} else { Write-Host 'The recorded process is not this BookMind server; no process was stopped.' }
