<#
Start Docker Desktop when the current user signs in, so the containers' `restart:
unless-stopped` policy brings HealthOS back after a reboot (Windows; install.ps1 -Autostart yes
runs this).

Adds only the current user's "Docker Desktop" value under
HKCU\Software\Microsoft\Windows\CurrentVersion\Run. It does not edit Docker settings, recreate
containers or touch volumes. An existing entry that already starts Docker Desktop is left as it
is. Until a user signs in, Docker Desktop and HealthOS are not running.

  powershell -ExecutionPolicy Bypass -File .\scripts\enable-docker-desktop-autostart.ps1 [-DryRun]
#>
param([switch]$DryRun)
$ErrorActionPreference = 'Stop'

if ($args.Count -gt 0) {
    [Console]::Error.WriteLine('usage: enable-docker-desktop-autostart.ps1 [-DryRun]')
    exit 2
}

$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$valueName = 'Docker Desktop'

function Find-DockerDesktop {
    $candidates = @()
    foreach ($base in @($env:ProgramFiles, $env:ProgramW6432)) {
        if ($base) { $candidates += (Join-Path $base 'Docker\Docker\Docker Desktop.exe') }
    }
    $installKey = 'HKLM:\SOFTWARE\Docker Inc.\Docker Desktop'
    if (Test-Path -LiteralPath $installKey) {
        $installed = (Get-ItemProperty -LiteralPath $installKey -ErrorAction SilentlyContinue).AppPath
        if ($installed) { $candidates = @((Join-Path $installed 'Docker Desktop.exe')) + $candidates }
    }
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    }
    return $null
}

$dockerDesktop = Find-DockerDesktop
if (-not $dockerDesktop) {
    if ($DryRun) {
        [Console]::Out.WriteLine('[dry-run] Docker Desktop was not found; would stop here with exit 1.')
        exit 0
    }
    [Console]::Error.WriteLine('Docker Desktop was not found. Install it first: winget install -e --id Docker.DockerDesktop')
    exit 1
}
$wanted = ('"{0}"' -f $dockerDesktop)

$current = $null
if (Test-Path -LiteralPath $runKey) {
    $current = (Get-ItemProperty -LiteralPath $runKey -Name $valueName -ErrorAction SilentlyContinue).$valueName
}
if ($current -and $current -like '*Docker Desktop.exe*') {
    [Console]::Out.WriteLine("Docker Desktop user-login startup already configured: $current")
    exit 0
}
if ($DryRun) {
    [Console]::Out.WriteLine("[dry-run] would set $runKey\$valueName = $wanted")
    exit 0
}
if (-not (Test-Path -LiteralPath $runKey)) { New-Item -Path $runKey -Force | Out-Null }
New-ItemProperty -LiteralPath $runKey -Name $valueName -Value $wanted -PropertyType String -Force | Out-Null
$configured = (Get-ItemProperty -LiteralPath $runKey -Name $valueName).$valueName
[Console]::Out.WriteLine("Docker Desktop user-login startup configured: $configured")
exit 0
