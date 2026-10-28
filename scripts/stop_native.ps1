$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $PSScriptRoot
foreach ($port in @(8018,8019)) {
    $listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $listener) { continue }
    $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener.OwningProcess)"
    if ($processInfo -and $processInfo.CommandLine.Contains($projectDir) -and ($processInfo.CommandLine.Contains('bookmind.main:app') -or $processInfo.CommandLine.Contains('next'))) {
        Stop-Process -Id $listener.OwningProcess
        Write-Host "BookMind process on $port stopped."
    } else { Write-Host "Port $port is not owned by this BookMind directory; skipped." }
}
