param([switch]$NoBrowser, [switch]$Rebuild)
$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'scripts\launch_native.ps1') @PSBoundParameters
