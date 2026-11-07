<#
Scheduled-task wrapper for scripts/backup.ps1 (the Windows counterpart of
deploy/systemd/healthos-backup.service and healthos-backup-failure.service).

Runs the backup, appends everything it prints to %LOCALAPPDATA%\HealtOS\logs\backup.log (the
log is only appended to, never truncated) and exits with the backup's exit code, so Task
Scheduler's "Last Run Result" shows a failure. On failure it also shows a message to the signed-in
user where msg.exe exists. The last result is always in
<BACKUP_HOST_DIR>\last-backup-status.json and at GET /health/backup.

Registered by deploy/windows/register-tasks.ps1; run it by hand to test the task:
  powershell -ExecutionPolicy Bypass -File deploy\windows\run-backup.ps1
#>
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$logDir = Get-HealtOSLogDir
if (-not (Test-Path -LiteralPath $logDir -PathType Container)) {
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
}
$log = Join-Path $logDir 'backup.log'
$utf8 = New-Object System.Text.UTF8Encoding($false)

function Add-LogText {
    param([string]$Text)
    [System.IO.File]::AppendAllText($log, $Text, $utf8)
}

$stamp = Get-HealtOSUtcStamp "yyyy'-'MM'-'dd'T'HH':'mm':'ss'Z'"
Add-LogText "=== $stamp HealtOS backup started`n"

$engine = (Get-Process -Id $PID).Path
$info = New-Object System.Diagnostics.ProcessStartInfo
$info.FileName = $engine
$info.Arguments = (@('-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File',
        (Join-Path (Join-Path $root 'scripts') 'backup.ps1')) |
    ForEach-Object { ConvertTo-HealtOSArgument $_ }) -join ' '
$info.UseShellExecute = $false
$info.CreateNoWindow = $true
$info.WorkingDirectory = $root
$info.RedirectStandardOutput = $true
$info.RedirectStandardError = $true
$info.StandardOutputEncoding = $utf8
$info.StandardErrorEncoding = $utf8
$process = [System.Diagnostics.Process]::Start($info)
# Read both streams concurrently so neither pipe can fill up and block the backup.
$stdout = $process.StandardOutput.ReadToEndAsync()
$stderr = $process.StandardError.ReadToEndAsync()
$process.WaitForExit()
Add-LogText $stderr.Result
Add-LogText $stdout.Result
$code = $process.ExitCode
$stamp = Get-HealtOSUtcStamp "yyyy'-'MM'-'dd'T'HH':'mm':'ss'Z'"
if ($code -eq 0) {
    Add-LogText "=== $stamp HealtOS backup finished`n"
} else {
    Add-LogText "=== $stamp HealtOS backup FAILED (exit $code). No backup was deleted.`n"
    $msg = Join-Path $env:SystemRoot 'System32\msg.exe'
    if ($env:SystemRoot -and (Test-Path -LiteralPath $msg)) {
        try {
            & $msg $env:USERNAME "HealtOS backup FAILED (exit $code). See $log and last-backup-status.json in the backups folder. No backup was deleted." 2>$null
        } catch {
            $null = $_
        }
    }
}
exit $code
