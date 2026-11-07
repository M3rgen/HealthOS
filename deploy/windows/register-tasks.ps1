<#
Registers the HealthOS scheduled tasks for the current Windows user (no administrator rights):

  \HealthOS\HealthOS Backup       daily at 03:30, runs deploy\windows\run-backup.ps1
                                  (scripts\backup.ps1); a missed run starts as soon as possible
                                  after the computer is back on (like systemd's Persistent=true)
  \HealthOS\HealthOS Umay bridge  at sign-in, runs deploy\windows\start-umay-bridge.ps1 (-Umay)

The Windows counterpart of deploy/systemd/healthos-backup.timer and healthos-bridge.service.
Both run only while you are signed in, like Docker Desktop itself. Registering again updates
the task in place; nothing else is changed and no file is deleted. A task of the same kind under
the misspelled name used up to 0.2.2 (\HealtOS\HealtOS Backup, \HealtOS\HealtOS Umay bridge) is
unregistered after its replacement is registered: a task is only a schedule, its log and the
backups stay.

  powershell -ExecutionPolicy Bypass -File deploy\windows\register-tasks.ps1 -Backup [-Umay] [-DryRun]

See them:   Get-ScheduledTask -TaskPath '\HealthOS\'
Run now:    Start-ScheduledTask -TaskPath '\HealthOS\' -TaskName 'HealthOS Backup'
Turn off:   Disable-ScheduledTask -TaskPath '\HealthOS\' -TaskName 'HealthOS Backup'
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
$TaskPath = '\HealthOS\'
$BackupTask = 'HealthOS Backup'
$BridgeTask = 'HealthOS Umay bridge'
$LegacyTaskPath = '\HealtOS\'
$LegacyTasks = @{ $BackupTask = 'HealtOS Backup'; $BridgeTask = 'HealtOS Umay bridge' }

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
            '-File', $path) | ForEach-Object { ConvertTo-HealthOSArgument $_ }) -join ' '
}

function Register-HealthOSTask {
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

function Unregister-LegacyTask {
    # The same task under the pre-rename name, if it exists, once the new one is registered.
    param([string]$Name)
    $legacy = $LegacyTasks[$Name]
    $found = $null
    if (Get-Command -Name Get-ScheduledTask -ErrorAction SilentlyContinue) {
        $found = Get-ScheduledTask -TaskPath $LegacyTaskPath -TaskName $legacy -ErrorAction SilentlyContinue
    }
    if (-not $found) { return }
    if ($DryRun) {
        [Console]::Out.WriteLine("[dry-run] Unregister-ScheduledTask $LegacyTaskPath$legacy (old name; replaced by $TaskPath$Name)")
        return
    }
    try {
        Unregister-ScheduledTask -TaskPath $LegacyTaskPath -TaskName $legacy -Confirm:$false
        [Console]::Out.WriteLine("Old scheduled task $LegacyTaskPath$legacy unregistered (replaced by $TaskPath$Name).")
    } catch {
        [Console]::Error.WriteLine("Could not unregister the old task $LegacyTaskPath$legacy; turn it off in Task Scheduler: $($_.Exception.Message)")
    }
}

if (-not (Test-HealthOSWindows) -and -not $DryRun) {
    [Console]::Error.WriteLine('register-tasks.ps1 needs Windows (Task Scheduler).')
    exit 2
}

if ($Backup) {
    $trigger = $null
    if (-not $DryRun) {
        $trigger = New-ScheduledTaskTrigger -Daily -At '03:30'
        $trigger.RandomDelay = 'PT5M'
    }
    Register-HealthOSTask -Name $BackupTask -Script 'run-backup.ps1' -Trigger $trigger `
        -Limit (New-TimeSpan -Hours 6) `
        -Description 'HealthOS encrypted PostgreSQL backup (scripts\backup.ps1). Keeps every backup; log in %LOCALAPPDATA%\HealthOS\logs\backup.log.'
    Unregister-LegacyTask $BackupTask
}
if ($Umay) {
    $trigger = $null
    if (-not $DryRun) {
        $user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
        $trigger.Delay = 'PT1M'
    }
    Register-HealthOSTask -Name $BridgeTask -Script 'start-umay-bridge.ps1' -Trigger $trigger `
        -Limit (New-TimeSpan -Minutes 10) `
        -Description 'HealthOS Umay bridge (scripts\start_codex_bridge.py) at sign-in; log in %LOCALAPPDATA%\HealthOS\logs\umay-bridge.log.'
    Unregister-LegacyTask $BridgeTask
}
exit 0
