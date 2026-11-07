# Shared helpers for the HealtOS Windows scripts: install.ps1, scripts/backup.ps1,
# scripts/restore.ps1 and deploy/windows/*.ps1. Dot-source it; it only defines functions and
# changes nothing by itself.
#
# Runs on Windows PowerShell 5.1 and PowerShell 7. Kept ASCII-only: Windows PowerShell 5.1
# reads a script without a byte order mark in the ANSI code page.
#
# Binary data (pg_dump output, age ciphertext) never goes through a PowerShell pipeline: those
# re-encode bytes as text. Processes are connected with System.Diagnostics.Process and a raw
# Stream.CopyToAsync between their standard streams instead.

function Test-HealtOSWindows {
    return [System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT
}

function Get-HealtOSConfigDir {
    # Per-user directory outside the checkout and outside .env for the backup key and
    # backup.env (the Windows counterpart of /etc/healthos).
    if (Test-HealtOSWindows) {
        return (Join-Path $env:APPDATA 'HealtOS')
    }
    return (Join-Path (Join-Path $HOME '.config') 'healtos')
}

function Get-HealtOSLogDir {
    if (Test-HealtOSWindows) {
        return (Join-Path (Join-Path $env:LOCALAPPDATA 'HealtOS') 'logs')
    }
    return (Join-Path (Join-Path (Join-Path $HOME '.local') 'state') 'healtos')
}

function Resolve-HealtOSPath {
    # Absolute path; a relative one is taken relative to $Base (not the process directory).
    param([string]$Path, [string]$Base)
    if ([System.IO.Path]::IsPathRooted($Path)) {
        return [System.IO.Path]::GetFullPath($Path)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $Base $Path))
}

function Find-HealtOSCommand {
    # Full path of an executable on PATH, or $null.
    param([string]$Name)
    $command = Get-Command -Name $Name -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -eq $command) { return $null }
    return $command.Path
}

function Get-HealtOSDotEnvValue {
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

function Import-HealtOSBackupEnv {
    # %APPDATA%\HealtOS\backup.env plays the role of systemd's EnvironmentFile: each KEY=value
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

function ConvertTo-HealtOSArgument {
    # Quote one argument for a Windows command line (CommandLineToArgvW rules; .NET applies the
    # same rules when it splits ProcessStartInfo.Arguments on other systems).
    param([string]$Value)
    if ($Value.Length -gt 0 -and $Value -notmatch '[\s"]') { return $Value }
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

function Start-HealtOSProcess {
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
    $info.Arguments = (@($ArgumentList | ForEach-Object { ConvertTo-HealtOSArgument $_ }) -join ' ')
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardInput = [bool]$RedirectInput
    $info.RedirectStandardOutput = [bool]$RedirectOutput
    $info.RedirectStandardError = $false
    if ($WorkingDirectory) { $info.WorkingDirectory = $WorkingDirectory }
    return [System.Diagnostics.Process]::Start($info)
}

function Wait-HealtOSTask {
    # Waits in short slices so Ctrl+C still reaches PowerShell. Returns $true on success.
    param($Task)
    try {
        while (-not $Task.Wait(250)) { }
        return $true
    } catch {
        return $false
    }
}

function Stop-HealtOSProcess {
    param($Process)
    if ($null -eq $Process) { return }
    try {
        if (-not $Process.HasExited) { $Process.Kill() }
    } catch {
        # Already gone.
        $null = $_
    }
}

function Invoke-HealtOSCapture {
    # Runs a command and returns its exit code and UTF-8 stdout as text (stderr inherited).
    param([string]$FilePath, [string[]]$ArgumentList = @(), [string]$WorkingDirectory = '')
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $FilePath
    $info.Arguments = (@($ArgumentList | ForEach-Object { ConvertTo-HealtOSArgument $_ }) -join ' ')
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

function Get-HealtOSCurrentSid {
    return [System.Security.Principal.WindowsIdentity]::GetCurrent().User
}

function Set-HealtOSPrivateAcl {
    # Current user only: inheritance off, every other entry removed, full control for the
    # current user. On other systems: mode 600 (700 for a directory).
    param([string]$Path, [switch]$Directory)
    if (-not (Test-HealtOSWindows)) {
        $mode = '600'
        if ($Directory) { $mode = '700' }
        & chmod $mode $Path
        if ($LASTEXITCODE -ne 0) { throw "chmod $mode $Path failed" }
        return
    }
    $sid = Get-HealtOSCurrentSid
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

function Get-HealtOSAclProblem {
    # '' when only the current user (plus SYSTEM and Administrators, which can read any file
    # anyway) may read the file; otherwise a short description of who else can.
    param([string]$Path)
    if (-not (Test-HealtOSWindows)) {
        $item = Get-Item -LiteralPath $Path -Force
        $mode = $item.UnixMode
        if ($null -eq $mode) { return '' }
        if ($mode.Substring(4, 6) -ne '------') { return "mode $mode allows group or others" }
        return ''
    }
    $mine = (Get-HealtOSCurrentSid).Value
    $trusted = @($mine, 'S-1-5-18', 'S-1-5-32-544')
    $read = [System.Security.AccessControl.FileSystemRights]::ReadData
    $acl = Get-Acl -LiteralPath $Path
    foreach ($rule in @($acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier]))) {
        if ($rule.AccessControlType -ne [System.Security.AccessControl.AccessControlType]::Allow) { continue }
        if (($rule.FileSystemRights -band $read) -eq 0) { continue }
        $sid = $rule.IdentityReference.Value
        if ($trusted -contains $sid) { continue }
        $name = $sid
        try {
            $name = $rule.IdentityReference.Translate([System.Security.Principal.NTAccount]).Value
        } catch {
            $null = $_
        }
        return "readable by $name"
    }
    return ''
}

function Get-HealtOSFreeBytes {
    # Free bytes available to the current user on the volume holding $Path, or $null.
    param([string]$Path)
    try {
        if (Test-HealtOSWindows) {
            $root = [System.IO.Path]::GetPathRoot($Path)
            if ($root -match '^[A-Za-z]:\\$') {
                return [int64](New-Object System.IO.DriveInfo($root)).AvailableFreeSpace
            }
            if (-not ('HealtOS.Disk' -as [type])) {
                Add-Type -Namespace HealtOS -Name Disk -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("kernel32.dll", CharSet = System.Runtime.InteropServices.CharSet.Unicode, SetLastError = true)]
public static extern bool GetDiskFreeSpaceEx(string directory, out ulong available, out ulong total, out ulong free);
'@
            }
            $available = [uint64]0
            $total = [uint64]0
            $free = [uint64]0
            if ([HealtOS.Disk]::GetDiskFreeSpaceEx($Path, [ref]$available, [ref]$total, [ref]$free)) {
                return [int64]$available
            }
            return $null
        }
        return [int64](New-Object System.IO.DriveInfo($Path)).AvailableFreeSpace
    } catch {
        return $null
    }
}

function Format-HealtOSGiB {
    param($Bytes)
    $gib = [decimal]$Bytes / [decimal]1073741824
    return $gib.ToString('0.00', [System.Globalization.CultureInfo]::InvariantCulture) + ' GiB'
}

function ConvertTo-HealtOSJsonString {
    # JSON string literal; control characters are dropped like backup.sh does.
    param([string]$Value)
    $clean = [regex]::Replace($Value, '[\x00-\x1f]', '')
    $clean = $clean.Replace('\', '\\').Replace('"', '\"')
    return '"' + $clean + '"'
}

function Get-HealtOSUtcStamp {
    param([string]$Format)
    return [System.DateTime]::UtcNow.ToString($Format, [System.Globalization.CultureInfo]::InvariantCulture)
}
