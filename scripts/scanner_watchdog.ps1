param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path,
    [int]$CheckIntervalSeconds = 30,
    [int]$RestartDelaySeconds = 10
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$runtime = Join-Path $ProjectRoot 'runtime'
New-Item -ItemType Directory -Force $runtime | Out-Null

$scannerPidFile = Join-Path $runtime 'scanner.pid'
$watchdogPidFile = Join-Path $runtime 'watchdog.pid'
$watchdogLog = Join-Path $runtime 'watchdog.log'
$scannerOut = Join-Path $runtime 'scanner.out.log'
$scannerErr = Join-Path $runtime 'scanner.err.log'
$cliPath = Join-Path $ProjectRoot 'cli.py'
$python = (Get-Command python).Source

Set-Content -LiteralPath $watchdogPidFile -Value $PID -Encoding UTF8

function Write-WatchdogLog {
    param([string]$Message)
    $line = '{0} {1}' -f (Get-Date).ToString('s'), $Message
    Add-Content -LiteralPath $watchdogLog -Value $line -Encoding UTF8
}

function Get-ManagedScannerProcess {
    if (-not (Test-Path -LiteralPath $scannerPidFile)) {
        return $null
    }
    $raw = (Get-Content -LiteralPath $scannerPidFile -Raw).Trim()
    $scannerPid = 0
    if (-not [int]::TryParse($raw, [ref]$scannerPid)) {
        Remove-Item -LiteralPath $scannerPidFile -Force -ErrorAction SilentlyContinue
        return $null
    }
    $process = Get-Process -Id $scannerPid -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        Remove-Item -LiteralPath $scannerPidFile -Force -ErrorAction SilentlyContinue
        return $null
    }
    return $process
}

function Start-ScannerProcess {
    Write-WatchdogLog "starting scanner"
    $process = Start-Process -FilePath $python `
        -ArgumentList @($cliPath, 'run') `
        -WorkingDirectory $ProjectRoot `
        -RedirectStandardOutput $scannerOut `
        -RedirectStandardError $scannerErr `
        -WindowStyle Hidden `
        -PassThru
    Set-Content -LiteralPath $scannerPidFile -Value $process.Id -Encoding UTF8
    Write-WatchdogLog "scanner started pid=$($process.Id)"
}

Write-WatchdogLog "watchdog started pid=$PID project=$ProjectRoot"

while ($true) {
    try {
        $process = Get-ManagedScannerProcess
        if ($null -eq $process) {
            Start-Sleep -Seconds $RestartDelaySeconds
            Start-ScannerProcess
        }
    }
    catch {
        Write-WatchdogLog "watchdog error: $($_.Exception.GetType().Name)"
    }
    Start-Sleep -Seconds $CheckIntervalSeconds
}
