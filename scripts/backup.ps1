<#
Encrypted, timestamped PostgreSQL backup for the Compose deployment on Windows (Docker Desktop).
The Windows counterpart of scripts/backup.sh, with the same behaviour and the same files.

Runs on the Docker host (Task Scheduler: deploy/windows/register-tasks.ps1, daily at 03:30).
The Compose `backup` profile streams `pg_dump --format=custom` to stdout and this script
encrypts the stream before anything touches the disk. The bytes are copied between processes
with System.Diagnostics.Process streams; they never pass through a PowerShell pipeline (which
would re-encode them as text and corrupt the dump).

Encryption key (never in .env, never next to the backups):
  1. BACKUP_AGE_RECIPIENT=age1...  -> age, public key only on the host (preferred)
  2. BACKUP_KEY_FILE (default %APPDATA%\HealthOS\backup.key, readable only by your account):
       an age identity (AGE-SECRET-KEY-...) -> age
       any other content                    -> gpg --symmetric passphrase
  Keep a copy of the key (or the age identity) off this computer, e.g. in a password manager:
  without it every backup is unreadable. age: winget install FiloSottile.age

Settings come from the environment, then %APPDATA%\HealthOS\backup.env (like systemd's
EnvironmentFile; applied only to variables the environment does not set), then the project's
.env file: BACKUP_HOST_DIR (default .\backups), BACKUP_STATUS_HOST_DIR (default
.\backup-status), BACKUP_RETENTION_DAYS, BACKUP_MIN_FREE_MB; BACKUP_AGE_RECIPIENT and
BACKUP_KEY_FILE only from the environment or backup.env. Installs up to 0.2.2 used the misspelled
%APPDATA%\HealtOS: while a file is missing in %APPDATA%\HealthOS, the old one is used.

Backups are kept forever. Nothing is ever deleted unless BACKUP_RETENTION_DAYS is explicitly set
(environment, else .env) to a positive whole number of days; unset, empty, 0, "never" or anything
else means never delete. Old backups are never deleted to make room.

Free-space guard: the script refuses to start (exit 1, no file written) when the backup
directory has less free space than BACKUP_MIN_FREE_MB, or by default twice the newest dump (at
least 1 GiB), or 5 GiB before the first dump. BACKUP_MIN_FREE_MB=0 disables the guard.

Every run writes <BACKUP_HOST_DIR>\last-backup-status.json (result, time, file, size, free
space, message) and the sanitized <BACKUP_STATUS_HOST_DIR>\status.json the app reads, on
success and on failure. stdout carries only the path of the new backup; everything else goes
to stderr.

  powershell -ExecutionPolicy Bypass -File .\scripts\backup.ps1
#>
$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
. (Join-Path (Join-Path (Join-Path $Root 'deploy') 'windows') 'common.ps1')

$GiB = [decimal]1073741824
$StatusName = 'last-backup-status.json'
$DotEnv = Join-Path $Root '.env'

Import-HealthOSBackupEnv (Get-HealthOSConfigFile 'backup.env')

function Write-Log {
    param([string]$Message)
    [Console]::Error.WriteLine("backup: $Message")
}

function Get-Setting {
    # Environment first (when non-empty), then .env.
    param([string]$Name)
    $value = [System.Environment]::GetEnvironmentVariable($Name)
    if ($value) { return $value }
    return (Get-HealthOSDotEnvValue -File $DotEnv -Name $Name)
}

$State = @{
    Output = ''
    Partial = ''
    StatusPartial = ''
    SizeBytes = $null
    RequiredBytes = $null
    RetentionLabel = 'never'
    PrunedCount = 0
    FailureReason = ''
    StatusWritten = $false
    Processes = @()
    Completed = $false
}

$BackupDir = Get-Setting 'BACKUP_HOST_DIR'
if (-not $BackupDir) { $BackupDir = './backups' }
$BackupDir = Resolve-HealthOSPath -Path $BackupDir -Base $Root
$StatusDir = Get-Setting 'BACKUP_STATUS_HOST_DIR'
if (-not $StatusDir) { $StatusDir = './backup-status' }
$StatusDir = Resolve-HealthOSPath -Path $StatusDir -Base $Root
$KeyFile = $env:BACKUP_KEY_FILE
if (-not $KeyFile) { $KeyFile = Get-HealthOSConfigFile 'backup.key' }
$Recipient = $env:BACKUP_AGE_RECIPIENT

function Fail {
    param([string]$Message)
    $State.FailureReason = $Message
    throw $Message
}

function ConvertTo-UtcIso {
    # Python's utc_timestamp(): an ISO 8601 time with an offset, normalised to UTC with "Z".
    param($Value)
    if ($Value -is [datetime]) {
        # PowerShell 7 ConvertFrom-Json turns ISO strings into DateTime values.
        $moment = [System.DateTimeOffset]::new($Value.ToUniversalTime())
    } elseif ($Value -is [string] -and $Value.Contains('T') -and
            $Value -match '(Z|[+-]\d\d:?\d\d)$') {
        $moment = [System.DateTimeOffset]::Parse($Value, [System.Globalization.CultureInfo]::InvariantCulture)
    } else {
        throw 'invalid timestamp'
    }
    $utc = $moment.UtcDateTime
    $text = $utc.ToString("yyyy'-'MM'-'dd'T'HH':'mm':'ss", [System.Globalization.CultureInfo]::InvariantCulture)
    $micro = [int](($utc.Ticks % 10000000) / 10)
    if ($micro -ne 0) { $text += '.' + $micro.ToString('000000') }
    return $text + 'Z'
}

function Read-PreviousSuccess {
    # Mirrors app.services.operations.backup_status.read_backup_status(): returns @{ State; Success }.
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return @{ State = 'missing'; Success = $null } }
    try {
        $bytes = [System.IO.File]::ReadAllBytes($Path)
        if ($bytes.Length -gt 65536) { throw 'oversize metadata' }
        $data = [System.Text.Encoding]::UTF8.GetString($bytes) | ConvertFrom-Json
        if ($data -isnot [System.Management.Automation.PSCustomObject]) { throw 'object required' }
        $names = @($data.PSObject.Properties | ForEach-Object { $_.Name })
        $legacy = $names -notcontains 'schema_version'
        if ($legacy) {
            $attempt = $data
        } else {
            if ($data.schema_version -isnot [int] -and $data.schema_version -isnot [long]) { throw 'unsupported schema' }
            if ($data.schema_version -ne 1) { throw 'unsupported schema' }
            if ($names -notcontains 'last_success_at') { throw 'missing success evidence' }
            $attempt = $data.last_attempt
        }
        if (@('success', 'failure') -cnotcontains $attempt.result) { throw 'invalid result' }
        $timestamp = ConvertTo-UtcIso $attempt.timestamp
        if ($legacy) {
            $success = $null
            if ($attempt.result -eq 'success') { $success = $timestamp }
        } else {
            $success = $data.last_success_at
            if ($null -ne $success) { $success = ConvertTo-UtcIso $success }
        }
        $invariant = [System.Globalization.CultureInfo]::InvariantCulture
        if ($null -ne $success -and
                [System.DateTimeOffset]::Parse($success, $invariant) -gt [System.DateTimeOffset]::Parse($timestamp, $invariant)) {
            throw 'success after attempt'
        }
        if ($attempt.result -eq 'success' -and $success -ne $timestamp) { throw 'inconsistent success' }
        return @{ State = 'available'; Success = $success }
    } catch {
        return @{ State = 'invalid'; Success = $null }
    }
}

function Write-AtomicText {
    # Write a sibling temporary file, flush it to disk, then replace the target in one step.
    param([string]$Directory, [string]$Target, [string]$Text, [string]$Prefix, [string]$UnixMode)
    $name = $Prefix + [System.IO.Path]::GetRandomFileName().Replace('.', '')
    $temporary = Join-Path $Directory $name
    $State.StatusPartial = $temporary
    $stream = New-Object System.IO.FileStream($temporary, [System.IO.FileMode]::CreateNew,
        [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
    try {
        $bytes = (New-Object System.Text.UTF8Encoding($false)).GetBytes($Text)
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
    } finally {
        $stream.Dispose()
    }
    if (-not (Test-HealthOSWindows) -and $UnixMode) { & chmod $UnixMode $temporary }
    if (Test-Path -LiteralPath $Target -PathType Leaf) {
        try {
            [System.IO.File]::Replace($temporary, $Target, [NullString]::Value)
        } catch {
            # A file shared into Docker Desktop can refuse ReplaceFile; copy over it instead.
            [System.IO.File]::Copy($temporary, $Target, $true)
            Remove-PartialFile $temporary
        }
    } else {
        [System.IO.File]::Move($temporary, $Target)
    }
    $State.StatusPartial = ''
}

function Publish-StatusMetadata {
    # The sanitized status.json the app reads (same content as scripts/write_backup_status.py).
    param([string]$Result, [string]$Timestamp)
    if (-not (Test-Path -LiteralPath $StatusDir -PathType Container)) {
        New-Item -ItemType Directory -Path $StatusDir -Force | Out-Null
    }
    if (-not (Test-HealthOSWindows)) { & chmod 755 $StatusDir }
    $lockPath = Join-Path $StatusDir '.writer.lock'
    $lock = $null
    for ($attempt = 0; $attempt -lt 120 -and $null -eq $lock; $attempt++) {
        try {
            $lock = New-Object System.IO.FileStream($lockPath, [System.IO.FileMode]::OpenOrCreate,
                [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
        } catch [System.IO.IOException] {
            Start-Sleep -Milliseconds 250
        }
    }
    if ($null -eq $lock) { throw 'status lock busy' }
    try {
        $path = Join-Path $StatusDir 'status.json'
        $previous = Read-PreviousSuccess $path
        if ($previous.State -eq 'missing') {
            $previous = Read-PreviousSuccess (Join-Path $BackupDir $StatusName)
        }
        $success = $previous.Success
        if ($Result -eq 'success') { $success = $Timestamp }
        $successJson = 'null'
        if ($null -ne $success) { $successJson = ConvertTo-HealthOSJsonString $success }
        $text = '{"schema_version": 1, "last_attempt": {"result": ' + (ConvertTo-HealthOSJsonString $Result) +
            ', "timestamp": ' + (ConvertTo-HealthOSJsonString $Timestamp) + '}, "last_success_at": ' +
            $successJson + "}`n"
        Write-AtomicText -Directory $StatusDir -Target $path -Text $text -Prefix '.status-' -UnixMode '644'
    } finally {
        $lock.Dispose()
    }
}

function Write-Status {
    # Write-Status RESULT MESSAGE; atomic replace, never fatal.
    param([string]$Result, [string]$Message)
    $timestamp = Get-HealthOSUtcStamp "yyyy'-'MM'-'dd'T'HH':'mm':'ss'Z'"
    try {
        Publish-StatusMetadata -Result $Result -Timestamp $timestamp
    } catch {
        Write-Log 'could not publish status metadata'
    }
    try {
        if (-not (Test-Path -LiteralPath $BackupDir -PathType Container)) {
            New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null
        }
    } catch {
        return
    }
    $free = Get-HealthOSFreeBytes $BackupDir
    $fileJson = 'null'
    if ($State.Output -and $Result -eq 'success') { $fileJson = ConvertTo-HealthOSJsonString $State.Output }
    $number = {
        param($Value)
        if ($null -eq $Value -or "$Value" -eq '') { return 'null' }
        return ([decimal]$Value).ToString('0', [System.Globalization.CultureInfo]::InvariantCulture)
    }
    $lines = @(
        '{',
        ('  "timestamp": ' + (ConvertTo-HealthOSJsonString $timestamp) + ','),
        ('  "result": ' + (ConvertTo-HealthOSJsonString $Result) + ','),
        ('  "message": ' + (ConvertTo-HealthOSJsonString $Message) + ','),
        ('  "file": ' + $fileJson + ','),
        ('  "size_bytes": ' + (& $number $State.SizeBytes) + ','),
        ('  "free_bytes": ' + (& $number $free) + ','),
        ('  "required_free_bytes": ' + (& $number $State.RequiredBytes) + ','),
        ('  "retention_days": ' + (ConvertTo-HealthOSJsonString $State.RetentionLabel) + ','),
        ('  "pruned_files": ' + $State.PrunedCount),
        '}'
    )
    try {
        Write-AtomicText -Directory $BackupDir -Target (Join-Path $BackupDir $StatusName) `
            -Text (($lines -join "`n") + "`n") -Prefix ('.' + $StatusName + '.tmp.') -UnixMode '600'
        $State.StatusWritten = $true
    } catch {
        Remove-PartialFile $State.StatusPartial
        $State.StatusPartial = ''
        Write-Log "could not write $(Join-Path $BackupDir $StatusName)"
    }
}

function Remove-PartialFile {
    # Only ever an unfinished temporary file this run created itself (never a backup).
    param([string]$Path)
    if (-not $Path) { return }
    try {
        if ([System.IO.File]::Exists($Path)) { [System.IO.File]::Delete($Path) }
    } catch {
        Write-Log "could not remove the unfinished file $Path"
    }
}

function Get-RetentionDays {
    # Retention is opt-in: old dumps are deleted only when BACKUP_RETENTION_DAYS is a positive
    # whole number of days. Set in the environment, the environment wins and .env is not read
    # (Windows cannot hold an empty environment variable, so there "set" means non-empty).
    # Anything else deletes nothing; an unrecognised value only warns. 36500 works and is
    # effectively never.
    if (Test-Path -LiteralPath 'Env:BACKUP_RETENTION_DAYS') {
        $value = [string]$env:BACKUP_RETENTION_DAYS
    } else {
        $value = Get-HealthOSDotEnvValue -File $DotEnv -Name 'BACKUP_RETENTION_DAYS'
    }
    if ($value -eq '' -or $value -ieq 'never') { return 0 }
    if ($value -notmatch '^[0-9]+$') {
        Write-Log "BACKUP_RETENTION_DAYS='$value' is not a positive whole number of days; keeping every backup"
        return 0
    }
    $digits = $value.TrimStart('0')
    # 0 keeps everything; more than 9 digits is beyond any lifetime.
    if ($digits -eq '' -or $digits.Length -gt 9) { return 0 }
    return [int]$digits
}

function Get-HealthDumps {
    # health-*.dump, health-*.dump.age and health-*.dump.gpg directly in the backup directory.
    if (-not (Test-Path -LiteralPath $BackupDir -PathType Container)) { return @() }
    return @(Get-ChildItem -LiteralPath $BackupDir -File -Force |
        Where-Object { $_.Name -match '^health-.*\.dump(\.age|\.gpg)?$' })
}

function Invoke-OptInRetention {
    # Only after a successful new backup, only health-* dumps (including legacy plaintext ones)
    # and only when BACKUP_RETENTION_DAYS opted in. Off by default.
    param([int]$Days, [string]$Keep)
    if ($Days -le 0) { return }
    $now = [System.DateTime]::UtcNow
    $pruned = @()
    foreach ($dump in Get-HealthDumps) {
        if ($dump.FullName -eq $Keep) { continue }
        # find -mtime +N: whole days of age greater than N.
        if ([math]::Floor(($now - $dump.LastWriteTimeUtc).TotalDays) -le $Days) { continue }
        [System.IO.File]::Delete($dump.FullName)
        $pruned += $dump.FullName
    }
    if ($pruned.Count -gt 0) {
        foreach ($path in $pruned) { [Console]::Error.WriteLine($path) }
        $State.PrunedCount = $pruned.Count
        Write-Log "BACKUP_RETENTION_DAYS=$Days deleted $($pruned.Count) old dump(s)"
    }
}

function Test-KeyFile {
    if (-not (Test-Path -LiteralPath $KeyFile -PathType Leaf)) { return $false }
    try {
        $content = [System.IO.File]::ReadAllText($KeyFile)
    } catch {
        Fail "$KeyFile is not readable by $([System.Environment]::UserName)"
    }
    $problem = Get-HealthOSAclProblem $KeyFile
    if ($problem) {
        Fail ("$KeyFile must be readable only by your account ($problem); run: " +
            "powershell -ExecutionPolicy Bypass -File deploy\windows\protect-file.ps1 `"$KeyFile`"")
    }
    if ($content.Length -eq 0) { Fail "$KeyFile is empty" }
    return $true
}

function Invoke-Backup {
    $retentionDays = Get-RetentionDays
    if ($retentionDays -gt 0) { $State.RetentionLabel = [string]$retentionDays }

    $minFreeMb = Get-Setting 'BACKUP_MIN_FREE_MB'
    if ($minFreeMb -and $minFreeMb -notmatch '^[0-9]+$') {
        Fail 'BACKUP_MIN_FREE_MB must be a whole number of MiB (0 disables the free-space guard)'
    }

    if ($Recipient) {
        $tool = Find-HealthOSCommand 'age'
        if (-not $tool) { Fail 'BACKUP_AGE_RECIPIENT is set but age is not installed (winget install FiloSottile.age)' }
        $method = 'age'
        $encrypt = @('--encrypt', '--recipient', $Recipient)
    } elseif (Test-KeyFile) {
        if ([System.IO.File]::ReadAllText($KeyFile) -match '(?m)^AGE-SECRET-KEY-') {
            $tool = Find-HealthOSCommand 'age'
            if (-not $tool) { Fail "$KeyFile is an age identity but age is not installed (winget install FiloSottile.age)" }
            $method = 'age'
            $encrypt = @('--encrypt', '--identity', $KeyFile)
        } else {
            $tool = Find-HealthOSCommand 'gpg'
            if (-not $tool) { Fail 'gpg is required for a passphrase key file (winget install GnuPG.Gpg4win)' }
            $method = 'gpg'
            $encrypt = @('--batch', '--quiet', '--yes', '--pinentry-mode', 'loopback', '--symmetric',
                '--cipher-algo', 'AES256', '--passphrase-file', $KeyFile, '--output', '-')
        }
    } else {
        Fail ("no backup key: set BACKUP_AGE_RECIPIENT (e.g. in $(Join-Path (Get-HealthOSConfigDir) 'backup.env')) " +
            "or create $KeyFile (readable only by your account); keys do not belong in .env")
    }

    $docker = Find-HealthOSCommand 'docker'
    if (-not $docker) { Fail 'docker was not found; install and start Docker Desktop' }

    try {
        if (-not (Test-Path -LiteralPath $BackupDir -PathType Container)) {
            New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null
        }
    } catch {
        Fail "cannot create $BackupDir"
    }
    if (-not (Test-HealthOSWindows)) { & chmod 700 $BackupDir 2>$null }

    # Free-space guard: refuse before anything is written; never delete to make room.
    $newest = Get-HealthDumps | Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
    if ($minFreeMb) {
        $State.RequiredBytes = [decimal]$minFreeMb * 1048576
        $basis = "BACKUP_MIN_FREE_MB=$minFreeMb"
    } elseif ($null -ne $newest) {
        $State.RequiredBytes = [decimal]$newest.Length * 2
        if ($State.RequiredBytes -lt $GiB) { $State.RequiredBytes = $GiB }
        $basis = 'twice the newest dump, at least 1 GiB'
    } else {
        $State.RequiredBytes = 5 * $GiB
        $basis = '5 GiB before the first dump'
    }
    if ($State.RequiredBytes -gt 0) {
        $freeBefore = Get-HealthOSFreeBytes $BackupDir
        if ($null -eq $freeBefore) { Fail "cannot read free space of $BackupDir" }
        if ([decimal]$freeBefore -lt $State.RequiredBytes) {
            Fail ("refusing to start: $(Format-HealthOSGiB $freeBefore) free in $BackupDir, need " +
                "$(Format-HealthOSGiB $State.RequiredBytes) ($basis). No backup was written or deleted. " +
                "Add disk space or move old backups to other storage (docs/RUNBOOK.md, 'Disk space for " +
                "backups'), or set BACKUP_MIN_FREE_MB.")
        }
    }

    $stamp = Get-HealthOSUtcStamp "yyyyMMdd'T'HHmmss'Z'"
    $target = Join-Path $BackupDir "health-$stamp.dump.$method"
    $State.Output = $target

    # Exclusively reserve a private temporary file beside the final name.
    $random = [System.IO.Path]::GetRandomFileName().Replace('.', '')
    $partial = Join-Path $BackupDir (".health-$stamp.dump.$method.$random.partial")
    try {
        $file = New-Object System.IO.FileStream($partial, [System.IO.FileMode]::CreateNew,
            [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
    } catch {
        Fail 'cannot allocate a private encrypted dump file'
    }
    $State.Partial = $partial
    if (-not (Test-HealthOSWindows)) { & chmod 600 $partial }

    $ok = $true
    try {
        $dump = Start-HealthOSProcess -FilePath $docker -WorkingDirectory $Root -RedirectInput -RedirectOutput `
            -ArgumentList @('compose', '--profile', 'backup', 'run', '--rm', '-T', 'backup')
        $State.Processes += $dump
        $dump.StandardInput.Close()
        $cipher = Start-HealthOSProcess -FilePath $tool -ArgumentList $encrypt -WorkingDirectory $Root `
            -RedirectInput -RedirectOutput
        $State.Processes += $cipher
        $toCipher = $dump.StandardOutput.BaseStream.CopyToAsync($cipher.StandardInput.BaseStream)
        $toFile = $cipher.StandardOutput.BaseStream.CopyToAsync($file)
        if (-not (Wait-HealthOSTask $toCipher)) { $ok = $false }
        try { $cipher.StandardInput.Close() } catch { $ok = $false }
        if (-not (Wait-HealthOSTask $toFile)) { $ok = $false }
        $dump.WaitForExit()
        $cipher.WaitForExit()
        if ($dump.ExitCode -ne 0 -or $cipher.ExitCode -ne 0) { $ok = $false }
        $file.Flush($true)
    } catch {
        $ok = $false
    } finally {
        $file.Dispose()
    }
    if (-not $ok) { Fail 'pg_dump or encryption failed; no backup was written' }
    if ((New-Object System.IO.FileInfo($partial)).Length -eq 0) { Fail 'encrypted dump is empty; no backup was written' }

    # Publish without ever replacing an existing name: File.Move fails when the target exists.
    $published = ''
    $candidate = $target
    for ($try = 0; $try -lt 10 -and -not $published; $try++) {
        if (-not (Test-Path -LiteralPath $candidate)) {
            try {
                [System.IO.File]::Move($partial, $candidate)
                $published = $candidate
                break
            } catch [System.IO.IOException] {
                $null = $_
            }
        }
        $candidate = Join-Path $BackupDir ("health-$stamp-" + [guid]::NewGuid().ToString('N') + ".dump.$method")
    }
    if (-not $published) { Fail 'cannot publish encrypted dump; existing backups were preserved' }
    $State.Partial = ''
    $State.Output = $published
    $State.SizeBytes = (New-Object System.IO.FileInfo($published)).Length

    Invoke-OptInRetention -Days $retentionDays -Keep $published

    $freeAfter = Get-HealthOSFreeBytes $BackupDir
    if ($null -eq $freeAfter) { $freeAfter = 0 }
    Write-Status -Result 'success' -Message 'backup written'
    Write-Log "wrote $published ($($State.SizeBytes) bytes); free space in ${BackupDir}: $(Format-HealthOSGiB $freeAfter)"
    [Console]::Out.WriteLine($published)
    $State.Completed = $true
}

$exitCode = 0
try {
    Invoke-Backup
} catch {
    $exitCode = 1
    if (-not $State.FailureReason) {
        $State.FailureReason = "backup.ps1 failed: $($_.Exception.Message)"
    }
    Write-Log $State.FailureReason
} finally {
    if ($exitCode -eq 0 -and -not $State.Completed) {
        # Ctrl+C or a stopped task: PowerShell runs finally blocks but no catch block.
        $exitCode = 130
        $State.FailureReason = 'backup.ps1 was interrupted; no backup was written'
    }
    foreach ($process in $State.Processes) { Stop-HealthOSProcess $process }
    Remove-PartialFile $State.Partial
    Remove-PartialFile $State.StatusPartial
    if ($exitCode -ne 0 -and -not $State.StatusWritten) {
        Write-Status -Result 'failure' -Message $State.FailureReason
    }
}
exit $exitCode
