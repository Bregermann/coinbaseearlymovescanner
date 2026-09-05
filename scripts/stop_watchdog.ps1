param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$runtime = Join-Path $ProjectRoot 'runtime'
$watchdogPidFile = Join-Path $runtime 'watchdog.pid'
$scannerPidFile = Join-Path $runtime 'scanner.pid'

function Stop-PidFileProcess {
    param([string]$Path, [string]$Name)
    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
    $raw = (Get-Content -LiteralPath $Path -Raw).Trim()
    $targetPid = 0
    if ([int]::TryParse($raw, [ref]$targetPid)) {
        $process = Get-Process -Id $targetPid -ErrorAction SilentlyContinue
        if ($null -ne $process) {
            Stop-Process -Id $targetPid -Force
            Write-Output "$Name stopped pid=$targetPid"
        }
    }
    Remove-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
}

Stop-PidFileProcess -Path $watchdogPidFile -Name 'watchdog'
Stop-PidFileProcess -Path $scannerPidFile -Name 'scanner'
