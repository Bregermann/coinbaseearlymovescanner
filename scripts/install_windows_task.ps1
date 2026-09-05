param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path,
    [string]$TaskName = 'Coinbase Early Move Scanner Watchdog'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$startScript = Join-Path $ProjectRoot 'scripts\start_watchdog.ps1'
$hostExe = (Get-Command pwsh -ErrorAction SilentlyContinue).Source
if (-not $hostExe) {
    $hostExe = (Get-Command powershell).Source
}

$argument = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -ProjectRoot "{1}"' -f $startScript, $ProjectRoot
$action = New-ScheduledTaskAction -Execute $hostExe -Argument $argument -WorkingDirectory $ProjectRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Description 'Keeps the Coinbase Early Move Scanner watchdog running after logon.' -Force | Out-Null
Write-Output "scheduled task installed: $TaskName"
