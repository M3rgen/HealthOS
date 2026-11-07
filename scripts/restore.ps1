<#
Restore a backup into a SEPARATE scratch database and print per-table row counts.
The Windows counterpart of scripts/restore.sh, with the same behaviour.

  powershell -ExecutionPolicy Bypass -File .\scripts\restore.ps1 backups\health-YYYYmmddTHHMMSSZ.dump.age [target_db]

The target (default health_restore) is dropped and recreated; the live `health` database is
never touched. Decryption keys:
  *.age  BACKUP_AGE_IDENTITY, else BACKUP_KEY_FILE (default %APPDATA%\HealthOS\backup.key, or
         the old %APPDATA%\HealtOS\backup.key while only that exists)
  *.gpg  BACKUP_KEY_FILE passphrase file
  *.dump legacy plaintext dump
The decrypted dump is streamed straight into pg_restore through process streams; it never
touches the disk and never passes through a PowerShell pipeline. A restored archive is only
usable by the app together with the same GOOGLE_TOKEN_ENCRYPTION_KEY that encrypted its OAuth
refresh tokens.
#>
# No [Parameter()] attributes: extra arguments then land in $args and get the usage message.
param(
    [string]$BackupFile = '',
    [string]$TargetDb = 'health_restore'
)
$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
. (Join-Path (Join-Path (Join-Path $Root 'deploy') 'windows') 'common.ps1')

function Stop-Restore {
    param([string]$Message)
    [Console]::Error.WriteLine("restore: $Message")
    exit 1
}

if (-not $BackupFile -or $args.Count -gt 0) {
    [Console]::Error.WriteLine('usage: restore.ps1 BACKUP_FILE [target_db]')
    exit 2
}

Import-HealthOSBackupEnv (Get-HealthOSConfigFile 'backup.env')

$backup = Resolve-HealthOSPath -Path $BackupFile -Base (Get-Location).ProviderPath
$target = $TargetDb
if (-not (Test-Path -LiteralPath $backup -PathType Leaf)) { Stop-Restore "no such backup: $BackupFile" }
if ($target -cnotmatch '^[a-z_][a-z0-9_]{0,62}$') { Stop-Restore 'target database name must match [a-z_][a-z0-9_]*' }
if (@('health', 'postgres', 'template0', 'template1') -ccontains $target) {
    Stop-Restore "refusing to restore over '$target'; pick a scratch database"
}
$keyFile = $env:BACKUP_KEY_FILE
if (-not $keyFile) { $keyFile = Get-HealthOSConfigFile 'backup.key' }

function Test-Readable {
    param([string]$Path)
    try {
        $stream = [System.IO.File]::OpenRead($Path)
        $stream.Dispose()
        return $true
    } catch {
        return $false
    }
}

$decryptTool = ''
$decryptArgs = @()
if ($backup.EndsWith('.age')) {
    $decryptTool = Find-HealthOSCommand 'age'
    if (-not $decryptTool) { Stop-Restore "age is required to decrypt $backup (winget install FiloSottile.age)" }
    $identity = $env:BACKUP_AGE_IDENTITY
    if (-not $identity) { $identity = $keyFile }
    if (-not (Test-Readable $identity)) { Stop-Restore "age identity $identity is not readable" }
    $decryptArgs = @('--decrypt', '--identity', $identity, $backup)
} elseif ($backup.EndsWith('.gpg')) {
    $decryptTool = Find-HealthOSCommand 'gpg'
    if (-not $decryptTool) { Stop-Restore "gpg is required to decrypt $backup" }
    if (-not (Test-Readable $keyFile)) { Stop-Restore "passphrase file $keyFile is not readable" }
    $decryptArgs = @('--batch', '--quiet', '--pinentry-mode', 'loopback', '--passphrase-file', $keyFile,
        '--decrypt', $backup)
} elseif (-not $backup.EndsWith('.dump')) {
    Stop-Restore "unknown backup type: $backup"
}

$docker = Find-HealthOSCommand 'docker'
if (-not $docker) { Stop-Restore 'docker was not found; install and start Docker Desktop' }

function Invoke-Docker {
    param([string[]]$Arguments)
    $process = Start-HealthOSProcess -FilePath $docker -ArgumentList $Arguments -WorkingDirectory $Root
    $process.WaitForExit()
    if ($process.ExitCode -ne 0) { Stop-Restore "docker $($Arguments -join ' ') failed" }
}

function Get-RowCounts {
    param([string]$Database, [string]$Sql)
    $result = Invoke-HealthOSCapture -FilePath $docker -WorkingDirectory $Root -ArgumentList @(
        'compose', 'exec', '-T', 'postgres', 'psql', '-U', 'health', '-d', $Database, '-v', 'ON_ERROR_STOP=1',
        '--tuples-only', '--no-align', "--field-separator=`t", '--command', $Sql)
    if ($result.ExitCode -ne 0) { return $null }
    return $result.Output
}

Invoke-Docker @('compose', 'exec', '-T', 'postgres', 'dropdb', '-U', 'health', '--if-exists', $target)
Invoke-Docker @('compose', 'exec', '-T', 'postgres', 'createdb', '-U', 'health', $target)

$restoreOk = $true
$source = $null
$decrypt = $null
$sink = $null
try {
    $sink = Start-HealthOSProcess -FilePath $docker -WorkingDirectory $Root -RedirectInput -ArgumentList @(
        'compose', 'exec', '-T', 'postgres', 'pg_restore', '-U', 'health', "--dbname=$target", '--no-owner',
        '--exit-on-error')
    if ($decryptTool) {
        $decrypt = Start-HealthOSProcess -FilePath $decryptTool -ArgumentList $decryptArgs -RedirectInput -RedirectOutput
        $decrypt.StandardInput.Close()
        $source = $decrypt.StandardOutput.BaseStream
    } else {
        $source = [System.IO.File]::OpenRead($backup)
    }
    if (-not (Wait-HealthOSTask $source.CopyToAsync($sink.StandardInput.BaseStream))) { $restoreOk = $false }
    try { $sink.StandardInput.Close() } catch { $restoreOk = $false }
    if ($null -ne $decrypt) {
        $decrypt.WaitForExit()
        if ($decrypt.ExitCode -ne 0) { $restoreOk = $false }
    }
    $sink.WaitForExit()
    if ($sink.ExitCode -ne 0) { $restoreOk = $false }
} catch {
    $restoreOk = $false
} finally {
    if ($null -eq $decrypt -and $null -ne $source) { $source.Dispose() }
    Stop-HealthOSProcess $decrypt
    Stop-HealthOSProcess $sink
}
if (-not $restoreOk) { Stop-Restore 'decryption or pg_restore failed' }

$countSql = @"
SELECT table_name, (xpath('/row/c/text()', query_to_xml(format(
  'SELECT count(*) AS c FROM %I.%I', table_schema, table_name), false, true, '')))[1]::text
  FROM information_schema.tables
  WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY table_name
"@ -replace "`r", ''

function ConvertFrom-Counts {
    # Validates a complete query result before any comparison: two fields, a name, a count, no
    # duplicate names. A query error or malformed count must never look like an equal archive.
    param([string]$Text)
    $counts = [ordered]@{}
    foreach ($line in ($Text -replace "`r", '').Split("`n")) {
        if ($line -eq '') { continue }
        $fields = $line.Split("`t")
        if ($fields.Count -ne 2 -or $fields[0] -eq '' -or $fields[1] -notmatch '^[0-9]+$' -or
                $counts.Contains($fields[0])) {
            Stop-Restore 'invalid row-count query result'
        }
        $counts[$fields[0]] = $fields[1]
    }
    return $counts
}

$restoredText = Get-RowCounts -Database $target -Sql $countSql
if ($null -eq $restoredText) { Stop-Restore 'restored row-count query failed' }
# A successful query with no rows means the dump restored nothing; never a valid drill.
if ($restoredText.Trim() -eq '') { Stop-Restore "restored database $target has no tables" }
$liveText = Get-RowCounts -Database 'health' -Sql $countSql
if ($null -eq $liveText) { Stop-Restore 'live row-count query failed' }
$restored = ConvertFrom-Counts $restoredText
$live = ConvertFrom-Counts $liveText

[Console]::Out.WriteLine("Restored $([System.IO.Path]::GetFileName($backup)) into database '$target'.")
[Console]::Out.WriteLine("table`trestored`tlive`tcomparison")
$tables = New-Object 'System.Collections.Generic.SortedSet[string]' ([System.StringComparer]::Ordinal)
foreach ($name in $restored.Keys) { [void]$tables.Add($name) }
foreach ($name in $live.Keys) { [void]$tables.Add($name) }
foreach ($name in $tables) {
    $inRestored = $restored.Contains($name)
    $inLive = $live.Contains($name)
    if (-not $inRestored) { $status = 'live_only' }
    elseif (-not $inLive) { $status = 'restored_only' }
    elseif ($restored[$name] -ceq $live[$name]) { $status = 'equal' }
    else { $status = 'different' }
    $left = '-'
    $right = '-'
    if ($inRestored) { $left = $restored[$name] }
    if ($inLive) { $right = $live[$name] }
    [Console]::Out.WriteLine("$name`t$left`t$right`t$status")
}
[Console]::Out.WriteLine('Counts compare a backup snapshot with the current live archive; differences alone do not imply corruption.')
$revision = Invoke-HealthOSCapture -FilePath $docker -WorkingDirectory $Root -ArgumentList @(
    'compose', 'exec', '-T', 'postgres', 'psql', '-U', 'health', '-d', $target, '-v', 'ON_ERROR_STOP=1',
    '--tuples-only', '--no-align', '--command', 'SELECT version_num FROM alembic_version')
if ($revision.ExitCode -ne 0) { Stop-Restore 'restored revision query failed' }
[Console]::Out.WriteLine("Alembic revision: $($revision.Output.Trim())")
[Console]::Out.WriteLine("Drop the scratch copy when done: docker compose exec -T postgres dropdb -U health $target")
exit 0
