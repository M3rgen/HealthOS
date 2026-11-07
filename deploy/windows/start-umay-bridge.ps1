<#
Starts the optional Umay (Codex) bridge at sign-in: the Windows counterpart of
deploy/systemd/healthos-bridge.service. Runs scripts/start_codex_bridge.py through
deploy/windows/umay_bridge_task.py, which starts the bridge detached on 127.0.0.1:8766 (Docker
Desktop forwards host.docker.internal there) or reuses a healthy one, so the app needs no restart
after a reboot.

.env is only read, never rewritten: CODEX_BRIDGE_TOKEN comes from .env (install.ps1 -Umay yes
writes it once) and is passed to the bridge in the environment, like the systemd unit's
EnvironmentFile. Without a token the task stops and logs why.

Python: the checkout's .venv (uv sync --frozen --no-dev) when present, otherwise
`uv run --frozen --no-dev` on a Python from Get-HealthOSBridgePython. The Codex CLI
must be installed with npm and signed in (`codex login`); see docs/legacy/WINDOWS.md. Output is
appended to %LOCALAPPDATA%\HealthOS\logs\umay-bridge.log; the bridge's own log is bridge.log in
%USERPROFILE%\.codex\healthos-bridge.

Registered by deploy/windows/register-tasks.ps1 -Umay; run it by hand to test:
  powershell -ExecutionPolicy Bypass -File deploy\windows\start-umay-bridge.ps1
#>
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$logDir = Get-HealthOSLogDir
if (-not (Test-Path -LiteralPath $logDir -PathType Container)) {
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
}
$log = Join-Path $logDir 'umay-bridge.log'
$utf8 = New-Object System.Text.UTF8Encoding($false)
$launcher = Join-Path $PSScriptRoot 'umay_bridge_task.py'

function Add-LogLine {
    param([string]$Text)
    $stamp = Get-HealthOSUtcStamp "yyyy'-'MM'-'dd'T'HH':'mm':'ss'Z'"
    [System.IO.File]::AppendAllText($log, "=== $stamp $Text`n", $utf8)
}

$token = Get-HealthOSDotEnvValue -File (Join-Path $root '.env') -Name 'CODEX_BRIDGE_TOKEN'
if (-not $token) {
    Add-LogLine 'CODEX_BRIDGE_TOKEN is empty in .env: run install.ps1 -Umay yes once. The bridge was not started.'
    exit 1
}

$python = Join-Path $root '.venv\Scripts\python.exe'
if (Test-Path -LiteralPath $python -PathType Leaf) {
    $file = $python
    $arguments = @($launcher)
} else {
    $file = Find-HealthOSCommand 'uv'
    if (-not $file) {
        Add-LogLine 'uv was not found (winget install -e --id astral-sh.uv)'
        exit 1
    }
    $arguments = @('run', '--frozen', '--no-dev', '--python', (Get-HealthOSBridgePython), '--project', $root, 'python', $launcher)
}

$info = New-Object System.Diagnostics.ProcessStartInfo
$info.FileName = $file
$info.Arguments = (@($arguments | ForEach-Object { ConvertTo-HealthOSArgument $_ }) -join ' ')
$info.UseShellExecute = $false
$info.CreateNoWindow = $true
$info.WorkingDirectory = $root
$info.RedirectStandardOutput = $true
$info.RedirectStandardError = $true
$info.StandardOutputEncoding = $utf8
$info.StandardErrorEncoding = $utf8
$info.EnvironmentVariables['PYTHONIOENCODING'] = 'utf-8'
# The token in .env wins over an older bridge.token, so the bridge and the app always agree.
$info.EnvironmentVariables['CODEX_BRIDGE_TOKEN'] = $token
$process = [System.Diagnostics.Process]::Start($info)
$stdout = $process.StandardOutput.ReadToEndAsync()
$stderr = $process.StandardError.ReadToEndAsync()
$process.WaitForExit()
$stamp = Get-HealthOSUtcStamp "yyyy'-'MM'-'dd'T'HH':'mm':'ss'Z'"
[System.IO.File]::AppendAllText($log,
    "=== $stamp exit $($process.ExitCode)`n" + $stdout.Result + $stderr.Result, $utf8)
exit $process.ExitCode
