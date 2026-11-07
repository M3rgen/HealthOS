<#
Make a file (or a directory) readable only by the current Windows account: inheritance off,
every other access entry removed, full control for you. The Windows counterpart of
`chmod 600` / `chmod 700`. It changes permissions only; the content is never touched.

  powershell -ExecutionPolicy Bypass -File deploy\windows\protect-file.ps1 "$env:APPDATA\HealtOS\backup.key"
#>
param([string]$Path = '')
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

if (-not $Path -or $args.Count -gt 0) {
    [Console]::Error.WriteLine('usage: protect-file.ps1 PATH')
    exit 2
}
if (-not (Test-Path -LiteralPath $Path)) {
    [Console]::Error.WriteLine("protect-file: no such file: $Path")
    exit 1
}
$isDirectory = Test-Path -LiteralPath $Path -PathType Container
Set-HealtOSPrivateAcl -Path $Path -Directory:$isDirectory
$problem = Get-HealtOSAclProblem $Path
if ($problem) {
    [Console]::Error.WriteLine("protect-file: $Path is still $problem")
    exit 1
}
[Console]::Out.WriteLine("$Path is now readable only by $([System.Environment]::UserName).")
exit 0
