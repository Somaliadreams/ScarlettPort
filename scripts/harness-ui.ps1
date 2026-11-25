<#
.SYNOPSIS
    Launches the ScarlettPort Harness Web UI & Agent Loop Control Plane.

.EXAMPLE
    .\scripts\harness-ui.ps1
.EXAMPLE
    .\scripts\harness-ui.ps1 -Game "D:\Games\MyDump" -Port 8765
.EXAMPLE
    .\scripts\harness-ui.ps1 -SelfTest
#>
param(
    [int]$Port = 8765,
    [string]$BindHost = '127.0.0.1',
    [string]$Game = '',
    [switch]$NoBrowser,
    [switch]$SelfTest
)

$ErrorActionPreference = 'Stop'
$serverScript = Join-Path $PSScriptRoot 'harness-ui-server.py'
if (-not (Test-Path -LiteralPath $serverScript)) {
    throw "Server script not found: $serverScript"
}

$pyCmd = Get-Command python -ErrorAction SilentlyContinue
$pyExe = $null
$pyPrefix = @()
if ($pyCmd) {
    $pyExe = $pyCmd.Source
} else {
    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        $pyExe = $pyLauncher.Source
        $pyPrefix = @('-3')
    }
}
if (-not $pyExe) {
    throw 'Python 3 is required to run the Harness UI (neither python nor py was found in PATH).'
}

$argsList = @()
$argsList += $pyPrefix
$argsList += @($serverScript, '--host', $BindHost, '--port', [string]$Port)
if ($Game) {
    $argsList += @('--game', $Game)
}
if ($SelfTest) {
    $argsList += '--self-test'
    & $pyExe @argsList
    exit $LASTEXITCODE
}

$url = "http://${BindHost}:${Port}"
if (-not $NoBrowser) {
    Start-Job -ScriptBlock {
        param($u)
        Start-Sleep -Milliseconds 900
        Start-Process $u
    } -ArgumentList $url | Out-Null
}

Write-Host "Starting ScarlettPort Harness UI at $url ..." -ForegroundColor Cyan
& $pyExe @argsList
exit $LASTEXITCODE
