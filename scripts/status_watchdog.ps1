param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$runtime = Join-Path $ProjectRoot 'runtime'
$watchdogPidFile = Join-Path $runtime 'watchdog.pid'
$scannerPidFile = Join-Path $runtime 'scanner.pid'

function Test-PidFileProcess {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return 'NOT RUNNING'
    }
    $raw = (Get-Content -LiteralPath $Path -Raw).Trim()
    $targetPid = 0
    if (-not [int]::TryParse($raw, [ref]$targetPid)) {
        return 'NOT RUNNING'
    }
    $process = Get-Process -Id $targetPid -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        return 'NOT RUNNING'
    }
    return "RUNNING pid=$targetPid"
}

Write-Output "watchdog: $(Test-PidFileProcess -Path $watchdogPidFile)"
Write-Output "scanner: $(Test-PidFileProcess -Path $scannerPidFile)"
