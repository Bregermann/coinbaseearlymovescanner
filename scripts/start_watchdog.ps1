param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$runtime = Join-Path $ProjectRoot 'runtime'
New-Item -ItemType Directory -Force $runtime | Out-Null
$watchdogPidFile = Join-Path $runtime 'watchdog.pid'
$watchdogScript = Join-Path $ProjectRoot 'scripts\scanner_watchdog.ps1'

if (Test-Path -LiteralPath $watchdogPidFile) {
    $raw = (Get-Content -LiteralPath $watchdogPidFile -Raw).Trim()
    $existingPid = 0
    if ([int]::TryParse($raw, [ref]$existingPid)) {
        $existing = Get-Process -Id $existingPid -ErrorAction SilentlyContinue
        if ($null -ne $existing) {
            Write-Output "watchdog already running pid=$existingPid"
            exit 0
        }
    }
}

$hostExe = (Get-Command pwsh -ErrorAction SilentlyContinue).Source
if (-not $hostExe) {
    $hostExe = (Get-Command powershell).Source
}

$process = Start-Process -FilePath $hostExe `
    -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $watchdogScript, '-ProjectRoot', $ProjectRoot) `
    -WorkingDirectory $ProjectRoot `
    -WindowStyle Hidden `
    -PassThru

Set-Content -LiteralPath $watchdogPidFile -Value $process.Id -Encoding UTF8
Write-Output "watchdog started pid=$($process.Id)"
