# Shared helpers for the HealthOS Windows scripts: install.ps1, scripts/backup.ps1,
# scripts/restore.ps1 and deploy/windows/*.ps1. Dot-source it; it only defines functions and
# changes nothing by itself.
#
# Runs on Windows PowerShell 5.1 and PowerShell 7. Kept ASCII-only: Windows PowerShell 5.1
# reads a script without a byte order mark in the ANSI code page.
#
# Binary data (pg_dump output, age ciphertext) never goes through a PowerShell pipeline: those
# re-encode bytes as text. Processes are connected with System.Diagnostics.Process and a raw
# Stream.CopyToAsync between their standard streams instead.

function Test-HealthOSWindows {
    return [System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT
}

function Get-HealthOSConfigDir {
    # Per-user directory outside the checkout and outside .env for the backup key and
    # backup.env (the Windows counterpart of /etc/healthos).
    if (Test-HealthOSWindows) {
        return (Join-Path $env:APPDATA 'HealthOS')
    }
    return (Join-Path (Join-Path $HOME '.config') 'healthos')
}

function Get-HealthOSLegacyConfigDir {
    # The same directory under the misspelled name used up to 0.2.2 (HealtOS). install.ps1
    # copies its files to Get-HealthOSConfigDir; nothing ever moves or deletes them.
    if (Test-HealthOSWindows) {
        return (Join-Path $env:APPDATA 'HealtOS')
    }
    return (Join-Path (Join-Path $HOME '.config') 'healtos')
}

function Get-HealthOSConfigFile {
    # Path of a file in the config directory: the new one, else the old (pre-rename) one while
    # only that exists, else the new one.
    param([string]$Name)
    $path = Join-Path (Get-HealthOSConfigDir) $Name
    if (Test-Path -LiteralPath $path) { return $path }
    $legacy = Join-Path (Get-HealthOSLegacyConfigDir) $Name
    if (Test-Path -LiteralPath $legacy -PathType Leaf) {
        [Console]::Error.WriteLine("note: $path not found; using the old path $legacy (re-run install.ps1 to copy it)")
        return $legacy
    }
    return $path
}

function Get-HealthOSBridgePython {
    # Python for the Umay bridge's .venv (uv sync --python): the newest Python a host offers can
    # be newer than pyproject.toml's requires-python (>=3.12,<3.14) and every wheel in uv.lock,
    # so uv picks or downloads one in this range. Same value as BRIDGE_PYTHON in install.sh.
    return '>=3.12,<3.14'
}

function Get-HealthOSLogDir {
    if (Test-HealthOSWindows) {
        return (Join-Path (Join-Path $env:LOCALAPPDATA 'HealthOS') 'logs')
    }
    return (Join-Path (Join-Path (Join-Path $HOME '.local') 'state') 'healthos')
}

function Resolve-HealthOSPath {
    # Absolute path; a relative one is taken relative to $Base (not the process directory).
    param([string]$Path, [string]$Base)
    if ([System.IO.Path]::IsPathRooted($Path)) {
        return [System.IO.Path]::GetFullPath($Path)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $Base $Path))
}

function Find-HealthOSCommand {
    # Full path of an executable on PATH, or $null.
    param([string]$Name)
    $command = Get-Command -Name $Name -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -eq $command) { return $null }
    return $command.Path
}

function Get-HealthOSDotEnvValue {
    # One plain KEY=value from a dotenv file without evaluating it (it holds secrets): the last
    # assignment wins, one pair of surrounding quotes is removed. Same rule as backup.sh; an
    # `export KEY=value` line counts as KEY (Compose reads it the same way).
    param([string]$File, [string]$Name)
    if (-not (Test-Path -LiteralPath $File -PathType Leaf)) { return '' }
    $value = ''
    $pattern = '^\s*(?:export\s+)?' + [regex]::Escape($Name) + '\s*=\s*(.*)$'
    foreach ($line in [System.IO.File]::ReadAllLines($File)) {
        $match = [regex]::Match($line.TrimEnd("`r"), $pattern)
        if ($match.Success) { $value = $match.Groups[1].Value }
    }
    $value = $value -replace '^["'']', ''
    $value = $value -replace '["'']$', ''
    return $value
}

function Import-HealthOSBackupEnv {
    # %APPDATA%\HealthOS\backup.env plays the role of systemd's EnvironmentFile: each KEY=value
    # is applied only when the variable is not already set in the environment.
    param([string]$File)
    if (-not (Test-Path -LiteralPath $File -PathType Leaf)) { return }
    foreach ($line in [System.IO.File]::ReadAllLines($File)) {
        $match = [regex]::Match($line.TrimEnd("`r"), '^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$')
        if (-not $match.Success) { continue }
        $name = $match.Groups[1].Value
        if (Test-Path -LiteralPath ("Env:" + $name)) { continue }
        $value = ($match.Groups[2].Value -replace '^["'']', '') -replace '["'']$', ''
        [System.Environment]::SetEnvironmentVariable($name, $value, 'Process')
    }
}

function ConvertTo-HealthOSArgument {
    # Quote one argument for a Windows command line (CommandLineToArgvW rules; .NET applies the
    # same rules when it splits ProcessStartInfo.Arguments on other systems). Arguments with
    # cmd.exe operators (< > & | ^ ( )) are quoted too: when the program is a .cmd or .bat shim,
    # cmd.exe reads the command line and would otherwise treat '<3.15' as a redirection. The
    # quotes are removed again for a .exe, so its argument is unchanged.
    param([string]$Value)
    if ($Value.Length -gt 0 -and $Value -notmatch '[\s"<>&|^()]') { return $Value }
    $builder = New-Object System.Text.StringBuilder
    [void]$builder.Append('"')
    $backslashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq [char]'\') {
            $backslashes++
            continue
        }
        if ($character -eq [char]'"') {
            [void]$builder.Append([string]::new([char]'\', 2 * $backslashes + 1))
            [void]$builder.Append('"')
        } else {
            if ($backslashes -gt 0) { [void]$builder.Append([string]::new([char]'\', $backslashes)) }
            [void]$builder.Append($character)
        }
        $backslashes = 0
    }
    if ($backslashes -gt 0) { [void]$builder.Append([string]::new([char]'\', 2 * $backslashes)) }
    [void]$builder.Append('"')
    return $builder.ToString()
}

function Start-HealthOSProcess {
    # Starts a process with optional redirected stdin/stdout; stderr is inherited so messages
    # of docker, age and gpg reach the caller's stderr (and the scheduled task's log).
    param(
        [string]$FilePath,
        [string[]]$ArgumentList = @(),
        [string]$WorkingDirectory = '',
        [switch]$RedirectInput,
        [switch]$RedirectOutput
    )
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $FilePath
    $info.Arguments = (@($ArgumentList | ForEach-Object { ConvertTo-HealthOSArgument $_ }) -join ' ')
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardInput = [bool]$RedirectInput
    $info.RedirectStandardOutput = [bool]$RedirectOutput
    $info.RedirectStandardError = $false
    if ($WorkingDirectory) { $info.WorkingDirectory = $WorkingDirectory }
    return [System.Diagnostics.Process]::Start($info)
}

function Wait-HealthOSTask {
    # Waits in short slices so Ctrl+C still reaches PowerShell. Returns $true on success.
    param($Task)
    try {
        while (-not $Task.Wait(250)) { }
        return $true
    } catch {
        return $false
    }
}

function Stop-HealthOSProcess {
    param($Process)
    if ($null -eq $Process) { return }
    try {
        if (-not $Process.HasExited) { $Process.Kill() }
    } catch {
        # Already gone.
        $null = $_
    }
}

function Invoke-HealthOSCapture {
    # Runs a command and returns its exit code and UTF-8 stdout as text (stderr inherited).
    param([string]$FilePath, [string[]]$ArgumentList = @(), [string]$WorkingDirectory = '')
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $FilePath
    $info.Arguments = (@($ArgumentList | ForEach-Object { ConvertTo-HealthOSArgument $_ }) -join ' ')
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.StandardOutputEncoding = New-Object System.Text.UTF8Encoding($false)
    if ($WorkingDirectory) { $info.WorkingDirectory = $WorkingDirectory }
    $process = [System.Diagnostics.Process]::Start($info)
    $output = $process.StandardOutput.ReadToEnd()
    $process.WaitForExit()
    return [pscustomobject]@{ ExitCode = $process.ExitCode; Output = $output }
}

function Get-HealthOSCurrentSid {
    return [System.Security.Principal.WindowsIdentity]::GetCurrent().User
}

function Set-HealthOSPrivateAcl {
    # Current user only: inheritance off, every other entry removed, full control for the
    # current user. On other systems: mode 600 (700 for a directory).
    param([string]$Path, [switch]$Directory)
    if (-not (Test-HealthOSWindows)) {
        $mode = '600'
        if ($Directory) { $mode = '700' }
        & chmod $mode $Path
        if ($LASTEXITCODE -ne 0) { throw "chmod $mode $Path failed" }
        return
    }
    $sid = Get-HealthOSCurrentSid
    $acl = Get-Acl -LiteralPath $Path
    $acl.SetAccessRuleProtection($true, $false)
    $rules = $acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier])
    foreach ($rule in @($rules)) { [void]$acl.RemoveAccessRuleSpecific($rule) }
    if ($Directory) {
        $inherit = [System.Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
    } else {
        $inherit = [System.Security.AccessControl.InheritanceFlags]::None
    }
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
        $sid,
        [System.Security.AccessControl.FileSystemRights]::FullControl,
        $inherit,
        [System.Security.AccessControl.PropagationFlags]::None,
        [System.Security.AccessControl.AccessControlType]::Allow)
    $acl.AddAccessRule($rule)
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function Get-HealthOSAclReader {
    # The decision behind Get-HealthOSAclProblem, over plain values so it is testable on any
    # host: the SID of the first entry that lets anyone but the current user, SYSTEM
    # (S-1-5-18) or Administrators (S-1-5-32-544) read the file, or '' when none does.
    # OWNER RIGHTS (S-1-3-4) applies to the file's owner, so it is fine only while the owner is
    # one of those three. Deny entries, entries without read access and inherit-only entries
    # (which do not apply to the object itself) never widen access.
    # Each entry: @{ Sid = 'S-1-...'; Allow = $true; Read = $true; InheritOnly = $false }
    param([string]$CurrentSid, [string]$OwnerSid, [object[]]$Entries = @())
    $trusted = @($CurrentSid, 'S-1-5-18', 'S-1-5-32-544')
    foreach ($entry in @($Entries)) {
        if ($null -eq $entry) { continue }
        if (-not $entry.Allow -or -not $entry.Read -or $entry.InheritOnly) { continue }
        $sid = [string]$entry.Sid
        if ($trusted -contains $sid) { continue }
        if ($sid -eq 'S-1-3-4' -and $OwnerSid -and $trusted -contains $OwnerSid) { continue }
        return $sid
    }
    return ''
}

function Get-HealthOSAclProblem {
    # '' when only the current user (plus SYSTEM and Administrators, which can read any file
    # anyway, and OWNER RIGHTS while one of them owns the file) may read the file; otherwise a
    # short description of who else can.
    param([string]$Path)
    if (-not (Test-HealthOSWindows)) {
        $item = Get-Item -LiteralPath $Path -Force
        $mode = $item.UnixMode
        if ($null -eq $mode) { return '' }
        if ($mode.Substring(4, 6) -ne '------') { return "mode $mode allows group or others" }
        return ''
    }
    $sidType = [System.Security.Principal.SecurityIdentifier]
    $read = [System.Security.AccessControl.FileSystemRights]::ReadData
    $allow = [System.Security.AccessControl.AccessControlType]::Allow
    $inheritOnly = [System.Security.AccessControl.PropagationFlags]::InheritOnly
    $acl = Get-Acl -LiteralPath $Path
    $owner = $null
    try { $owner = $acl.GetOwner($sidType) } catch { $null = $_ }
    $ownerSid = ''
    if ($owner) { $ownerSid = $owner.Value }
    $entries = @(foreach ($rule in @($acl.GetAccessRules($true, $true, $sidType))) {
            @{
                Sid = $rule.IdentityReference.Value
                Allow = ($rule.AccessControlType -eq $allow)
                Read = (($rule.FileSystemRights -band $read) -ne 0)
                InheritOnly = (($rule.PropagationFlags -band $inheritOnly) -ne 0)
            }
        })
    $sid = Get-HealthOSAclReader -CurrentSid (Get-HealthOSCurrentSid).Value -OwnerSid $ownerSid -Entries $entries
    if (-not $sid) { return '' }
    $name = Get-HealthOSSidName $sid
    if ($sid -eq 'S-1-3-4') { $name = "$name, owner $(Get-HealthOSSidName $ownerSid)" }
    return "readable by $name"
}

function Get-HealthOSSidName {
    # DOMAIN\name for a SID when Windows can translate it, otherwise the SID itself.
    param([string]$Sid)
    if (-not $Sid) { return 'unknown' }
    try {
        $identity = New-Object System.Security.Principal.SecurityIdentifier($Sid)
        return $identity.Translate([System.Security.Principal.NTAccount]).Value
    } catch {
        return $Sid
    }
}

function Get-HealthOSFreeBytes {
    # Free bytes available to the current user on the volume holding $Path, or $null.
    param([string]$Path)
    try {
        if (Test-HealthOSWindows) {
            $root = [System.IO.Path]::GetPathRoot($Path)
            if ($root -match '^[A-Za-z]:\\$') {
                return [int64](New-Object System.IO.DriveInfo($root)).AvailableFreeSpace
            }
            if (-not ('HealthOS.Disk' -as [type])) {
                Add-Type -Namespace HealthOS -Name Disk -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("kernel32.dll", CharSet = System.Runtime.InteropServices.CharSet.Unicode, SetLastError = true)]
public static extern bool GetDiskFreeSpaceEx(string directory, out ulong available, out ulong total, out ulong free);
'@
            }
            $available = [uint64]0
            $total = [uint64]0
            $free = [uint64]0
            if ([HealthOS.Disk]::GetDiskFreeSpaceEx($Path, [ref]$available, [ref]$total, [ref]$free)) {
                return [int64]$available
            }
            return $null
        }
        return [int64](New-Object System.IO.DriveInfo($Path)).AvailableFreeSpace
    } catch {
        return $null
    }
}

function Format-HealthOSGiB {
    param($Bytes)
    $gib = [decimal]$Bytes / [decimal]1073741824
    return $gib.ToString('0.00', [System.Globalization.CultureInfo]::InvariantCulture) + ' GiB'
}

function ConvertTo-HealthOSJsonString {
    # JSON string literal; control characters are dropped like backup.sh does.
    param([string]$Value)
    $clean = [regex]::Replace($Value, '[\x00-\x1f]', '')
    $clean = $clean.Replace('\', '\\').Replace('"', '\"')
    return '"' + $clean + '"'
}

function Get-HealthOSUtcStamp {
    param([string]$Format)
    return [System.DateTime]::UtcNow.ToString($Format, [System.Globalization.CultureInfo]::InvariantCulture)
}
