<#
Registers the HealtOS scheduled tasks for the current Windows user (no administrator rights):

  \HealtOS\HealtOS Backup       daily at 03:30, runs deploy\windows\run-backup.ps1
                                (scripts\backup.ps1); a missed run starts as soon as possible
                                after the computer is back on (like systemd's Persistent=true)
  \HealtOS\HealtOS Umay bridge  at sign-in, runs deploy\windows\start-umay-bridge.ps1 (-Umay)

The Windows counterpart of deploy/systemd/healthos-backup.timer and healthos-bridge.service.
Both run only while you are signed in, like Docker Desktop itself. Registering again updates
the task in place; nothing else is changed and no file is deleted.

  powershell -ExecutionPolicy Bypass -File deploy\windows\register-tasks.ps1 -Backup [-Umay] [-DryRun]

See them:   Get-ScheduledTask -TaskPath '\HealtOS\'
Run now:    Start-ScheduledTask -TaskPath '\HealtOS\' -TaskName 'HealtOS Backup'
Turn off:   Disable-ScheduledTask -TaskPath '\HealtOS\' -TaskName 'HealtOS Backup'
#>
param(
    [switch]$Backup,
    [switch]$Umay,
    [switch]$DryRun,
    [string]$RepoDir = ''
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

if ($args.Count -gt 0) {
    [Console]::Error.WriteLine('usage: register-tasks.ps1 [-Backup] [-Umay] [-DryRun] [-RepoDir PATH]')
    exit 2
}
if (-not $RepoDir) { $RepoDir = Split-Path -Parent (Split-Path -Parent $PSScriptRoot) }
$RepoDir = [System.IO.Path]::GetFullPath($RepoDir)
$TaskPath = '\HealtOS\'
$BackupTask = 'HealtOS Backup'
$BridgeTask = 'HealtOS Umay bridge'

# Windows PowerShell 5.1 ships with every Windows 10/11; the scripts run on it and on PowerShell 7.
$engine = '%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe'
if ($env:SystemRoot) {
    $engine = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
}

function Get-TaskArguments {
    param([string]$Script)
    $path = Join-Path (Join-Path (Join-Path $RepoDir 'deploy') 'windows') $Script
    # -ExecutionPolicy Bypass -File: the task runs under the machine's default policy
    # (Restricted on a stock PC), which would block the script otherwise.
    return (@('-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden', '-ExecutionPolicy', 'Bypass',
            '-File', $path) | ForEach-Object { ConvertTo-HealtOSArgument $_ }) -join ' '
}

function Register-HealtOSTask {
    param([string]$Name, [string]$Script, $Trigger, [string]$Description, [System.TimeSpan]$Limit)
    $arguments = Get-TaskArguments $Script
    if ($DryRun) {
        [Console]::Out.WriteLine("[dry-run] Register-ScheduledTask $TaskPath$Name : $engine $arguments")
        return
    }
    $user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $action = New-ScheduledTaskAction -Execute $engine -Argument $arguments -WorkingDirectory $RepoDir
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit $Limit
    $task = New-ScheduledTask -Action $action -Trigger $Trigger -Principal $principal -Settings $settings `
        -Description $Description
    Register-ScheduledTask -TaskPath $TaskPath -TaskName $Name -InputObject $task -Force | Out-Null
    [Console]::Out.WriteLine("Scheduled task $TaskPath$Name registered.")
}

if (-not (Test-HealtOSWindows) -and -not $DryRun) {
    [Console]::Error.WriteLine('register-tasks.ps1 needs Windows (Task Scheduler).')
    exit 2
}

if ($Backup) {
    $trigger = $null
    if (-not $DryRun) {
        $trigger = New-ScheduledTaskTrigger -Daily -At '03:30'
        $trigger.RandomDelay = 'PT5M'
    }
    Register-HealtOSTask -Name $BackupTask -Script 'run-backup.ps1' -Trigger $trigger `
        -Limit (New-TimeSpan -Hours 6) `
        -Description 'HealtOS encrypted PostgreSQL backup (scripts\backup.ps1). Keeps every backup; log in %LOCALAPPDATA%\HealtOS\logs\backup.log.'
}
if ($Umay) {
    $trigger = $null
    if (-not $DryRun) {
        $user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
        $trigger.Delay = 'PT1M'
    }
    Register-HealtOSTask -Name $BridgeTask -Script 'start-umay-bridge.ps1' -Trigger $trigger `
        -Limit (New-TimeSpan -Minutes 10) `
        -Description 'HealtOS Umay bridge (scripts\start_codex_bridge.py) at sign-in; log in %LOCALAPPDATA%\HealtOS\logs\umay-bridge.log.'
}
exit 0
