<#
HealtOS installer for Windows 10/11 with Docker Desktop (WSL 2 backend).
Windows PowerShell 5.1 and PowerShell 7. The Windows counterpart of install.sh: same options,
same questions, same .env rules.

  irm https://raw.githubusercontent.com/M3rgen/HealtOS/main/install.ps1 | iex
  & ([scriptblock]::Create((irm https://raw.githubusercontent.com/M3rgen/HealtOS/v0.2.2/install.ps1))) -Ref v0.2.2
  powershell -ExecutionPolicy Bypass -File .\install.ps1 [-DryRun] [-Help]

Safety: .env is never overwritten (it is copied to .env.bak-<UTC stamp> first and existing
secrets are never replaced), no volume, backup or file is ever deleted, `docker compose down -v`
is never run. Running it again is safe. Exit codes: 0 ok, 1 a step failed, 2 bad arguments or
unsupported environment.

This file is ASCII-only (Windows PowerShell 5.1 reads scripts without a byte order mark in the
ANSI code page): Turkish text is written with \uXXXX escapes and decoded at run time.
#>
param(
    [string]$Lang = '',
    [string]$Platform = '',
    [string]$Mode = '',
    [string]$Sources = '',
    [string]$Dir = '',
    [string]$Ref = '',
    [string]$GarminKeyFile = '',
    [string]$GoogleClientId = '',
    [string]$GoogleClientSecret = '',
    [string]$AppleReceiver = '',
    [string]$Backups = '',
    [string]$Autostart = '',
    [string]$Umay = '',
    [string]$EnvOut = '',
    [switch]$NonInteractive,
    [switch]$DryRun,
    [switch]$NoStart,
    [switch]$Help
)
# A stock Windows PC has the Restricted (or AllSigned) execution policy, which would block the
# cloned install.ps1, deploy\windows\common.ps1 and the scripts they run. Bypass for this
# process only: no administrator rights, nothing is stored. (A policy set by Group Policy still
# wins; PowerShell 7 on macOS/Linux has no execution policy.)
try { Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force -ErrorAction Stop } catch { $null = $_ }
$HEALTOS_INSTALLER_VERSION = "0.2.2"

$HealtOSRepoUrl = 'https://github.com/M3rgen/HealtOS.git'
$HealtOSRawUrl = 'https://raw.githubusercontent.com/M3rgen/HealtOS'
$HealtOSInstallerFile = $MyInvocation.MyCommand.Path
$HealtOSExtraArgs = @($args)
$HealtOSPlatforms = @('linux', 'raspberrypi', 'nas', 'macos', 'windows', 'vps')
# COMPOSE_FILE entries the installer manages; every other entry is the user's and is kept.
$HealtOSManagedCompose = @('docker-compose.yml', 'docker-compose.dev.yml', 'docker-compose.garmin.yml',
    'docker-compose.apple.yml', 'docker-compose.windows.yml')

# ---------------------------------------------------------------------------------------------
# Output and questions

function M {
    # English or Turkish text; Turkish is stored with \uXXXX escapes (see the header).
    param([string]$En, [string]$Tr)
    if ($script:InstallLang -eq 'tr') { return [regex]::Unescape($Tr) }
    return $En
}

function Say {
    param([string]$Text, [string]$Color = '')
    if ($Color) { Write-Host $Text -ForegroundColor $Color } else { Write-Host $Text }
}

function Say-Step {
    param([string]$Text)
    Write-Host ''
    Write-Host ('== ' + $Text) -ForegroundColor Cyan
}

function Say-Warn {
    param([string]$Text)
    Write-Host ((M 'Warning: ' 'Uyar\u0131: ') + $Text) -ForegroundColor Yellow
}

function Say-Plan {
    # Dry run: every action that would change something is printed instead of done.
    param([string]$Text)
    Write-Host ('[dry-run] ' + $Text) -ForegroundColor DarkCyan
}

function Stop-Installer {
    param([int]$Code, [string]$Message)
    $exception = New-Object System.Exception($Message)
    $record = New-Object System.Management.Automation.ErrorRecord($exception, 'HealtOSInstallerExit',
        [System.Management.Automation.ErrorCategory]::NotSpecified, $Code)
    throw $record
}

function Read-Answer {
    param([string]$Question, [string]$Default)
    if ($script:Unattended) { return $Default }
    $suffix = ''
    if ($Default -ne '') { $suffix = " [$Default]" }
    $answer = Read-Host ($Question + $suffix)
    if ($null -eq $answer) { return $Default }
    $answer = $answer.Trim()
    if ($answer -eq '') { return $Default }
    return $answer
}

function Read-YesNo {
    param([string]$Question, [string]$Default)
    while ($true) {
        $hint = M 'yes/no' 'evet/hay\u0131r'
        $answer = Read-Answer "$Question ($hint)" (ConvertTo-YesNoLabel $Default)
        $value = ConvertFrom-YesNo $answer
        if ($value) { return $value }
        Say (M 'Please answer yes or no.' 'L\u00fctfen evet ya da hay\u0131r yaz\u0131n.') Yellow
    }
}

function ConvertTo-YesNoLabel {
    param([string]$Value)
    if ($script:InstallLang -eq 'tr') {
        if ($Value -eq 'yes') { return 'evet' }
        return [regex]::Unescape('hay\u0131r')
    }
    return $Value
}

function ConvertFrom-YesNo {
    param([string]$Value)
    $text = ([string]$Value).Trim().ToLowerInvariant()
    if (@('yes', 'y', 'true', '1', 'evet', 'e') -contains $text) { return 'yes' }
    if (@('no', 'n', 'false', '0', 'h', [regex]::Unescape('hay\u0131r'), 'hayir') -contains $text) { return 'no' }
    return ''
}

# ---------------------------------------------------------------------------------------------
# Help

function Show-Usage {
    $english = @'
HealtOS installer for Windows 10/11 (Docker Desktop, WSL 2 backend)

Usage: .\install.ps1 [options]
  irm https://raw.githubusercontent.com/M3rgen/HealtOS/main/install.ps1 | iex

Options (the same as install.sh):
  -Lang en|tr                  language (asked; English preselected)
  -Platform linux|raspberrypi|nas|macos|windows|vps
                               target platform; this script installs `windows`, the others are
                               set up on that machine with install.sh (default: windows)
  -Mode trial|full             trial: local look, no Google needed (default)
                               full: production mode, needs Google and webhook values
  -Sources google,garmin,apple data sources, comma separated, or `none`
                               (default: google in full mode, none in trial)
  -Dir PATH                    checkout directory (default: this checkout, else %USERPROFILE%\healtos)
  -Ref REF                     git ref to clone when bootstrapping (default: main)
  -GarminKeyFile PATH          Garmin Connect Personal API key file
  -GoogleClientId ID           Google OAuth client ID
  -GoogleClientSecret S        Google OAuth client secret
  -AppleReceiver yes|no        Health Auto Export receiver (default: no)
  -Backups yes|no              daily encrypted backup at 03:30 (default: yes in full, no in trial)
  -Autostart yes|no            start Docker Desktop at sign-in (default: yes)
  -Umay yes|no                 Umay bridge as a sign-in task (default: no)
  -NonInteractive              never ask; use the options and the defaults
  -DryRun                      print every action, write the .env it would write to -EnvOut
                               (default <dir>\.env.dry-run); no Docker, no tasks, no clone
  -EnvOut PATH                 where -DryRun writes its .env
  -NoStart                     configure only, do not start the stack
  -Help                        this help

Never deleted or overwritten: .env (copied to .env.bak-<UTC stamp> first), secrets, volumes,
backups. Exit codes: 0 ok, 1 a step failed, 2 bad arguments or unsupported environment.
Linux server is the recommended platform for an always-on install.
'@
    $turkish = @'
Windows 10/11 i\u00e7in HealtOS kurulumu (Docker Desktop, WSL 2 altyap\u0131s\u0131)

Kullan\u0131m: .\\install.ps1 [se\u00e7enekler]
  irm https://raw.githubusercontent.com/M3rgen/HealtOS/main/install.ps1 | iex

Se\u00e7enekler (install.sh ile ayn\u0131):
  -Lang en|tr                  dil (sorulur; \u0130ngilizce \u00f6nse\u00e7ili)
  -Platform linux|raspberrypi|nas|macos|windows|vps
                               hedef platform; bu betik `windows` kurar, di\u011ferleri o makinede
                               install.sh ile kurulur (varsay\u0131lan: windows)
  -Mode trial|full             trial: yerel deneme, Google gerekmez (varsay\u0131lan)
                               full: \u00fcretim kipi, Google ve webhook de\u011ferleri gerekir
  -Sources google,garmin,apple veri kaynaklar\u0131, virg\u00fclle ya da `none`
                               (varsay\u0131lan: full kipte google, trial kipte none)
  -Dir YOL                     kurulum klas\u00f6r\u00fc (varsay\u0131lan: bu klon, yoksa %USERPROFILE%\\healtos)
  -Ref REF                     klonlanacak git ref'i (varsay\u0131lan: main)
  -GarminKeyFile YOL           Garmin Connect Personal API anahtar dosyas\u0131
  -GoogleClientId ID           Google OAuth istemci kimli\u011fi
  -GoogleClientSecret S        Google OAuth istemci s\u0131rr\u0131
  -AppleReceiver yes|no        Health Auto Export al\u0131c\u0131s\u0131 (varsay\u0131lan: no)
  -Backups yes|no              her g\u00fcn 03:30'da \u015fifreli yedek (varsay\u0131lan: full'da yes, trial'da no)
  -Autostart yes|no            oturum a\u00e7\u0131l\u0131nca Docker Desktop'u ba\u015flat (varsay\u0131lan: yes)
  -Umay yes|no                 Umay k\u00f6pr\u00fcs\u00fc oturum g\u00f6revi olarak (varsay\u0131lan: no)
  -NonInteractive              hi\u00e7 sorma; se\u00e7enekleri ve varsay\u0131lanlar\u0131 kullan
  -DryRun                      her ad\u0131m\u0131 yaz, yazaca\u011f\u0131 .env'i -EnvOut'a yaz
                               (varsay\u0131lan <klas\u00f6r>\\.env.dry-run); Docker, g\u00f6rev, klon yok
  -EnvOut YOL                  -DryRun'\u0131n .env'i yazaca\u011f\u0131 yer
  -NoStart                     yaln\u0131z yap\u0131land\u0131r, y\u0131\u011f\u0131n\u0131 ba\u015flatma
  -Help                        bu yard\u0131m

Asla silinmez ve \u00fczerine yaz\u0131lmaz: .env (\u00f6nce .env.bak-<UTC damgas\u0131> olarak kopyalan\u0131r),
s\u0131rlar, birimler, yedekler. \u00c7\u0131k\u0131\u015f kodlar\u0131: 0 tamam, 1 bir ad\u0131m ba\u015far\u0131s\u0131z,
2 hatal\u0131 se\u00e7enek ya da desteklenmeyen ortam.
S\u00fcrekli a\u00e7\u0131k bir kurulum i\u00e7in \u00f6nerilen platform Linux sunucudur.
'@
    # Both languages unless -Lang picked one.
    $chosen = ([string]$Lang).ToLowerInvariant()
    if ($chosen -ne 'tr') { Write-Host $english }
    if ($chosen -ne 'en') {
        if ($chosen -ne 'tr') { Write-Host '' }
        Write-Host ([regex]::Unescape($turkish))
    }
}

# ---------------------------------------------------------------------------------------------
# Arguments

function Test-Arguments {
    if ($HealtOSExtraArgs.Count -gt 0) {
        Stop-Installer 2 ((M 'unknown option or argument: {0} (see -Help)' 'bilinmeyen se\u00e7enek ya da arg\u00fcman: {0} (-Help''e bak\u0131n)') -f ($HealtOSExtraArgs -join ' '))
    }
    if ($Lang -and @('en', 'tr') -notcontains $Lang.ToLowerInvariant()) {
        Stop-Installer 2 ((M '-Lang must be en or tr, not {0}' '-Lang en ya da tr olmal\u0131, {0} de\u011fil') -f $Lang)
    }
    if ($Platform -and $HealtOSPlatforms -notcontains $Platform.ToLowerInvariant()) {
        Stop-Installer 2 ((M '-Platform must be one of {0}' '-Platform \u015funlardan biri olmal\u0131: {0}') -f ($HealtOSPlatforms -join ', '))
    }
    if ($Mode -and @('trial', 'full') -notcontains $Mode.ToLowerInvariant()) {
        Stop-Installer 2 (M '-Mode must be trial or full' '-Mode trial ya da full olmal\u0131')
    }
    if ($Sources) { [void](ConvertTo-SourceList $Sources $true) }
    foreach ($pair in @(@('-AppleReceiver', $AppleReceiver), @('-Backups', $Backups),
            @('-Autostart', $Autostart), @('-Umay', $Umay))) {
        if ($pair[1] -and @('yes', 'no') -notcontains $pair[1].ToLowerInvariant()) {
            Stop-Installer 2 ((M '{0} must be yes or no' '{0} yes ya da no olmal\u0131') -f $pair[0])
        }
    }
    if ($EnvOut -and -not $DryRun) {
        Stop-Installer 2 (M '-EnvOut only works with -DryRun' '-EnvOut yaln\u0131z -DryRun ile \u00e7al\u0131\u015f\u0131r')
    }
    # An explicit flag is honoured or fails (without -GarminKeyFile a missing default key file
    # only leaves Garmin off).
    if ($GarminKeyFile -and -not (Test-Path -LiteralPath (Resolve-UserPath $GarminKeyFile) -PathType Leaf)) {
        Stop-Installer 2 ((M '-GarminKeyFile {0} does not exist. Save your Garmin Personal API key in that file (one line) and run the installer again, or leave -GarminKeyFile out.' '-GarminKeyFile {0} yok. Garmin Personal API anahtar\u0131n\u0131z\u0131 bu dosyaya (tek sat\u0131r) kaydedip kurulumu yeniden \u00e7al\u0131\u015ft\u0131r\u0131n ya da -GarminKeyFile vermeyin.') -f (Resolve-UserPath $GarminKeyFile))
    }
}

function ConvertTo-SourceList {
    # 'google,garmin' or '1,3' -> @('google','garmin'); 'none' -> @(). $Strict: exit 2 on errors.
    param([string]$Text, [bool]$Strict)
    $names = @{ '1' = 'google'; '2' = 'garmin'; '3' = 'apple'; '4' = 'none'; '0' = 'none' }
    $result = @()
    $sawNone = $false
    foreach ($part in ($Text -split '[,\s]+')) {
        $item = $part.Trim().ToLowerInvariant()
        if ($item -eq '') { continue }
        if ($names.ContainsKey($item)) { $item = $names[$item] }
        if (@('google', 'garmin', 'apple') -contains $item) {
            if ($result -notcontains $item) { $result += $item }
        } elseif ($item -eq 'none') {
            $sawNone = $true
        } else {
            if ($Strict) {
                Stop-Installer 2 ((M '-Sources: unknown source {0} (google, garmin, apple or none)' '-Sources: bilinmeyen kaynak {0} (google, garmin, apple ya da none)') -f $part)
            }
            return $null
        }
    }
    if ($sawNone -and $result.Count -gt 0) {
        if ($Strict) {
            Stop-Installer 2 (M '-Sources: none cannot be combined with other sources' '-Sources: none ba\u015fka kaynakla birlikte kullan\u0131lamaz')
        }
        return $null
    }
    return , $result
}

# ---------------------------------------------------------------------------------------------
# Host facts

function Test-IsWindowsHost {
    return [System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT
}

function Get-HostLabel {
    if (Test-IsWindowsHost) { return 'Windows' }
    if ($IsMacOS) { return 'macOS' }
    return 'Linux'
}

function Test-Checkout {
    param([string]$Path)
    if (-not $Path) { return $false }
    # Same test as install.sh.
    $required = @((Join-Path $Path 'docker-compose.yml'), (Join-Path $Path '.env.example'),
        (Join-Path (Join-Path $Path 'scripts') 'backup.sh'))
    foreach ($candidate in $required) {
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { return $false }
    }
    return $true
}

function Get-DefaultDir {
    if ((Test-IsWindowsHost) -and $env:USERPROFILE) { return (Join-Path $env:USERPROFILE 'healtos') }
    return (Join-Path $HOME 'healtos')
}

function Find-Tool {
    param([string]$Name)
    $command = Get-Command -Name $Name -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -eq $command) { return $null }
    return $command.Path
}

# ---------------------------------------------------------------------------------------------
# Bootstrap: clone when not run from a checkout, then run the cloned installer

function Resolve-Checkout {
    $scriptDir = ''
    if ($HealtOSInstallerFile) { $scriptDir = Split-Path -Parent $HealtOSInstallerFile }
    if ($Dir) {
        $target = [System.IO.Path]::GetFullPath($ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Dir))
    } elseif (Test-Checkout $scriptDir) {
        return $scriptDir
    } elseif (Test-Checkout (Get-Location).ProviderPath) {
        return (Get-Location).ProviderPath
    } else {
        $target = Get-DefaultDir
        $target = Read-Answer (M 'Install directory (HealtOS will be cloned here)' 'Kurulum dizini (HealtOS buraya klonlan\u0131r)') $target
        $target = [System.IO.Path]::GetFullPath($ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($target))
    }
    if (Test-Checkout $target) { return $target }
    Invoke-Bootstrap $target
    return ''
}

function Invoke-Bootstrap {
    param([string]$Target)
    $ref = $Ref
    if (-not $ref) { $ref = 'main' }
    Say-Step (M 'Getting HealtOS' 'HealtOS indiriliyor')
    if ((Test-Path -LiteralPath $Target) -and @(Get-ChildItem -LiteralPath $Target -Force).Count -gt 0) {
        Stop-Installer 2 ((M '{0} exists, is not empty and is not a HealtOS checkout; nothing was changed. Choose another -Dir.' '{0} var, bo\u015f de\u011fil ve bir HealtOS klonu de\u011fil; hi\u00e7bir \u015fey de\u011fi\u015ftirilmedi. Ba\u015fka bir -Dir se\u00e7in.') -f $Target)
    }
    $git = Find-Tool 'git'
    if ($DryRun) {
        if (-not $git) { Say-Warn (M 'git was not found; install it with: winget install -e --id Git.Git' 'git bulunamad\u0131; \u015funu kurun: winget install -e --id Git.Git') }
        Say-Plan "git clone --branch $ref $HealtOSRepoUrl `"$Target`""
        Say-Plan ((M 'run {0} from the clone with the same options' 'klondaki {0} ayn\u0131 se\u00e7eneklerle \u00e7al\u0131\u015ft\u0131r\u0131l\u0131r') -f (Join-Path $Target 'install.ps1'))
        Say (M 'Dry run: nothing was cloned. Run the installer inside a checkout with -DryRun to preview the .env as well.' 'Deneme \u00e7al\u0131\u015fmas\u0131: hi\u00e7bir \u015fey klonlanmad\u0131. .env \u00f6nizlemesi i\u00e7in kurulumu bir klonun i\u00e7inde -DryRun ile \u00e7al\u0131\u015ft\u0131r\u0131n.')
        Stop-Installer 0 ''
    }
    if (-not $git) {
        Stop-Installer 2 (M 'git is required to download HealtOS. Install it with: winget install -e --id Git.Git (or https://git-scm.com/download/win), open a new PowerShell window and run the installer again.' 'HealtOS''u indirmek i\u00e7in git gerekli. \u015eununla kurun: winget install -e --id Git.Git (ya da https://git-scm.com/download/win), yeni bir PowerShell penceresi a\u00e7\u0131p kurulumu yeniden \u00e7al\u0131\u015ft\u0131r\u0131n.')
    }
    Say ((M 'Cloning {0} ({1}) into {2}' '{0} ({1}) {2} klas\u00f6r\u00fcne klonlan\u0131yor') -f $HealtOSRepoUrl, $ref, $Target)
    & $git clone --branch $ref $HealtOSRepoUrl $Target
    if ($LASTEXITCODE -ne 0) { Stop-Installer 1 (M 'git clone failed.' 'git clone ba\u015far\u0131s\u0131z oldu.') }
    $cloned = Join-Path $Target 'install.ps1'
    if (-not (Test-Path -LiteralPath $cloned -PathType Leaf)) {
        Stop-Installer 1 ((M 'The clone at {0} has no install.ps1 (ref {1} is too old).' '{0} klonunda install.ps1 yok ({1} ref''i \u00e7ok eski).') -f $Target, $ref)
    }
    $forward = @{}
    foreach ($key in $script:BoundParameters.Keys) { $forward[$key] = $script:BoundParameters[$key] }
    $forward['Dir'] = $Target
    $forward['Lang'] = $script:InstallLang
    & $cloned @forward
    Stop-Installer $LASTEXITCODE ''
}

# ---------------------------------------------------------------------------------------------
# Wizard

function Read-Menu {
    # Numbered menu; returns the chosen number (1-based). $Items holds English/Turkish pairs.
    param([string]$TitleEn, [string]$TitleTr, [int]$Default, [string[]]$Items)
    $count = $Items.Count / 2
    if ($script:Unattended) { return $Default }
    Write-Host ''
    Write-Host (M $TitleEn $TitleTr) -ForegroundColor Cyan
    for ($i = 0; $i -lt $count; $i++) {
        # Items may hold \uXXXX escapes in both languages (the Turkish option is always shown).
        Write-Host ('  {0}) {1}' -f ($i + 1), [regex]::Unescape((M $Items[2 * $i] $Items[2 * $i + 1])))
    }
    while ($true) {
        $answer = Read-Answer (M 'Choose' 'Se\u00e7in') ([string]$Default)
        $index = 0
        if ([int]::TryParse($answer, [ref]$index) -and $index -ge 1 -and $index -le $count) { return $index }
        Say ((M 'Please type a number from 1 to {0}.' 'L\u00fctfen 1 ile {0} aras\u0131nda bir say\u0131 yaz\u0131n.') -f $count) Yellow
    }
}

function Read-Secret {
    # Hidden input; empty when unattended.
    param([string]$Question)
    if ($script:Unattended) { return '' }
    $secure = Read-Host -Prompt $Question -AsSecureString
    if ($null -eq $secure -or $secure.Length -eq 0) { return '' }
    $pointer = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        return [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer).Trim()
    } finally {
        [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
}

function Add-Note {
    param([string]$Text)
    $script:Notes.Add($Text)
}

function Select-Language {
    if ($Lang) { $script:InstallLang = $Lang.ToLowerInvariant(); return }
    $script:InstallLang = 'en'
    if ($script:Unattended) { return }
    $choice = Read-Menu 'Language / Dil' 'Language / Dil' 1 @('English', 'English', 'T\u00fcrk\u00e7e', 'T\u00fcrk\u00e7e')
    if ($choice -eq 2) { $script:InstallLang = 'tr' }
}

function Select-Platform {
    # All six platforms are listed. This script installs `windows`; the others are set up on
    # their own machine with install.sh (like install.sh refuses `windows`).
    $chosen = ''
    if ($Platform) {
        $chosen = $Platform.ToLowerInvariant()
    } else {
        $index = Read-Menu 'Where will HealtOS run?' 'HealtOS nerede \u00e7al\u0131\u015facak?' 5 @(
            'Linux server / VM (Debian, Ubuntu, Fedora...; Proxmox VM/LXC) - recommended for an always-on install',
            'Linux sunucu / sanal makine (Debian, Ubuntu, Fedora...; Proxmox VM/LXC) - s\u00fcrekli a\u00e7\u0131k kurulum i\u00e7in \u00f6nerilen',
            'Raspberry Pi / ARM64 board - 64-bit OS required; 4 GB+ RAM recommended; first build is slow',
            'Raspberry Pi / ARM64 kart - 64 bit i\u015fletim sistemi \u015fart; 4 GB+ RAM \u00f6nerilir; ilk derleme yava\u015f',
            'NAS (Synology Container Manager, QNAP Container Station, Unraid, TrueNAS SCALE) - set up on the NAS',
            'NAS (Synology Container Manager, QNAP Container Station, Unraid, TrueNAS SCALE) - NAS''ta kurulur',
            'macOS (Apple Silicon or Intel) with Docker Desktop, OrbStack or Colima - uses install.sh',
            'macOS (Apple Silicon ya da Intel), Docker Desktop, OrbStack ya da Colima ile - install.sh kullan\u0131l\u0131r',
            'Windows 10/11 with Docker Desktop (WSL 2) - this PC',
            'Windows 10/11, Docker Desktop (WSL 2) ile - bu bilgisayar',
            'Cloud VPS - like Linux; open the dashboard through an SSH tunnel',
            'Bulut VPS - Linux gibi; panel SSH t\u00fcneliyle a\u00e7\u0131l\u0131r')
        $chosen = $HealtOSPlatforms[$index - 1]
    }
    if ($chosen -ne 'windows') {
        Show-OtherPlatform $chosen
        Stop-Installer 2 ((M 'Platform {0} cannot be installed on this {1} computer with install.ps1. Run the installer on the target machine.' '{0} platformu bu {1} bilgisayar\u0131na install.ps1 ile kurulamaz. Kurulumu hedef makinede \u00e7al\u0131\u015ft\u0131r\u0131n.') -f $chosen, (Get-HostLabel))
    }
    $script:InstallPlatform = $chosen
}

function Show-OtherPlatform {
    param([string]$Chosen)
    $oneLiner = "curl -fsSL $HealtOSRawUrl/main/install.sh | bash"
    switch ($Chosen) {
        'nas' {
            Say (M 'A NAS is set up through its own web interface: follow the NAS section of docs/INSTALL.md. Over SSH on the NAS you can run:' 'NAS kendi web aray\u00fcz\u00fcyle kurulur: docs/INSTALL.tr.md i\u00e7indeki NAS b\u00f6l\u00fcm\u00fcn\u00fc izleyin. NAS''ta SSH ile \u015funu \u00e7al\u0131\u015ft\u0131rabilirsiniz:')
            Say ('  ' + $oneLiner + ' -s -- --platform nas')
        }
        'vps' {
            Say (M 'Run this on the VPS over SSH; open the dashboard afterwards through an SSH tunnel (ssh -N -L 8000:127.0.0.1:8000 user@server):' 'Bunu VPS''te SSH ile \u00e7al\u0131\u015ft\u0131r\u0131n; paneli sonra SSH t\u00fcneliyle a\u00e7\u0131n (ssh -N -L 8000:127.0.0.1:8000 kullanici@sunucu):')
            Say ('  ' + $oneLiner)
        }
        default {
            Say ((M 'Run this on that machine ({0}):' 'Bunu o makinede ({0}) \u00e7al\u0131\u015ft\u0131r\u0131n:') -f $Chosen)
            Say ('  ' + $oneLiner)
        }
    }
}

function Get-DefaultMode {
    # A re-run keeps the mode of the existing .env: COMPOSE_FILE without the development
    # override is a full install. No .env or no COMPOSE_FILE: trial.
    if (-not $script:EnvExisted -or -not (Get-EnvValue 'COMPOSE_FILE')) { return 'trial' }
    foreach ($entry in (Get-ComposeEntries)) {
        if ((ConvertTo-ComposeName $entry) -eq 'docker-compose.dev.yml') { return 'trial' }
    }
    return 'full'
}

function Select-Mode {
    if ($Mode) { $script:InstallMode = $Mode.ToLowerInvariant(); return }
    $default = Get-DefaultMode
    $preselected = 1
    if ($default -eq 'full') {
        $preselected = 2
        Say (M 'The existing .env is a full install (COMPOSE_FILE without docker-compose.dev.yml); full mode is preselected. -Mode trial changes it.' 'Var olan .env tam kurulum (COMPOSE_FILE i\u00e7inde docker-compose.dev.yml yok); tam kip \u00f6nse\u00e7ili. -Mode trial ile de\u011fi\u015fir.')
    }
    $index = Read-Menu 'Mode' 'Kip' $preselected @(
        'Trial - look around locally; development override; no Google account needed',
        'Deneme - yerelde g\u00f6z at\u0131n; geli\u015ftirme ayar\u0131; Google hesab\u0131 gerekmez',
        'Full - production mode; needs Google OAuth and webhook values; daily backups',
        'Tam - \u00fcretim kipi; Google OAuth ve webhook de\u011ferleri gerekir; g\u00fcnl\u00fck yedek')
    $script:InstallMode = 'trial'
    if ($index -eq 2) { $script:InstallMode = 'full' }
}

function Select-Sources {
    $list = @()
    if ($Sources) {
        $list = ConvertTo-SourceList $Sources $true
    } elseif ($GarminKeyFile -or $GoogleClientId -or $GoogleClientSecret -or $AppleReceiver -eq 'yes') {
        $list = @()
    } elseif (-not $script:Unattended) {
        $default = '4'
        if ($script:InstallMode -eq 'full') { $default = '1' }
        Write-Host ''
        Write-Host (M 'Data sources (wearables) - pick one or more, e.g. 1,3' 'Veri kaynaklar\u0131 (giyilebilirler) - bir ya da daha fazla se\u00e7in, \u00f6rn. 1,3') -ForegroundColor Cyan
        Say ('  1) ' + (M 'Google Health API v4 (Fitbit, Pixel Watch, Google Health app) - OAuth client; required in full mode' 'Google Health API v4 (Fitbit, Pixel Watch, Google Health uygulamas\u0131) - OAuth istemcisi; tam kipte \u015fart'))
        Say ('  2) ' + (M 'Garmin Connect Personal API - read-only pull; needs your Personal API key file' 'Garmin Connect Personal API - salt okunur \u00e7ekme; Personal API anahtar dosyan\u0131z gerekir'))
        Say ('  3) ' + (M 'Apple Watch / Apple Health - export.zip upload works as is; optional Health Auto Export receiver' 'Apple Watch / Apple Sa\u011fl\u0131k - export.zip y\u00fckleme haz\u0131r; iste\u011fe ba\u011fl\u0131 Health Auto Export al\u0131c\u0131s\u0131'))
        Say ('  4) ' + (M 'None for now - manual entries, empty dashboard' '\u015eimdilik hi\u00e7biri - elle giri\u015fler, bo\u015f panel'))
        while ($true) {
            $answer = Read-Answer (M 'Choose' 'Se\u00e7in') $default
            $parsed = ConvertTo-SourceList $answer $false
            if ($null -ne $parsed) { $list = $parsed; break }
            Say (M 'Enter numbers 1-4 separated by commas (4 alone for none).' '1-4 aras\u0131 numaralar\u0131 virg\u00fclle girin (hi\u00e7biri i\u00e7in yaln\u0131z 4).') Yellow
        }
    } elseif ($script:InstallMode -eq 'full') {
        $list = @('google')
    }
    # Flags imply their source.
    if ($GarminKeyFile -and $list -notcontains 'garmin') { $list += 'garmin' }
    if (($GoogleClientId -or $GoogleClientSecret) -and $list -notcontains 'google') { $list += 'google' }
    if ($AppleReceiver -eq 'yes' -and $list -notcontains 'apple') { $list += 'apple' }
    $script:InstallSources = @($list)
}

function Select-SourceDetails {
    $script:GoogleId = $GoogleClientId
    $script:GoogleSecret = $GoogleClientSecret
    $script:GarminKey = $GarminKeyFile
    $script:AppleReceiverChoice = 'no'
    if ($AppleReceiver) { $script:AppleReceiverChoice = $AppleReceiver.ToLowerInvariant() }
    $script:WebhookHost = ''
    $script:AppleEndpoint = ''
    if ($script:InstallSources -contains 'google' -and -not $script:Unattended -and
            -not (Get-EnvValue 'GOOGLE_CLIENT_ID') -and -not $script:GoogleId) {
        Say ''
        Say (M 'Google: create an OAuth ''Web application'' client in Google Cloud (README step 3) with redirect URI' 'Google: Google Cloud''da ''Web uygulamas\u0131'' OAuth istemcisi olu\u015fturun (README 3. ad\u0131m); y\u00f6nlendirme URI''si')
        Say ('  ' + (Get-RedirectUri))
        $script:GoogleId = Read-Answer (M 'Client ID (empty: add later)' '\u0130stemci kimli\u011fi (bo\u015f: sonra eklenir)') ''
        if ($script:GoogleId -and -not $script:GoogleSecret) {
            $script:GoogleSecret = Read-Secret (M 'Client secret (hidden)' '\u0130stemci gizli anahtar\u0131 (gizli)')
        }
    }
    if ($script:InstallSources -contains 'garmin') {
        if (-not $script:GarminKey) {
            $script:GarminKey = Get-EnvValue 'GARMIN_API_KEY_HOST_FILE'
            if (-not $script:GarminKey) {
                $script:GarminKey = Join-Path (Join-Path (Join-Path (Get-UserHome) '.config') 'garmin') 'connect-personal-api-key'
            }
            $script:GarminKey = Read-Answer (M 'Garmin Personal API key file' 'Garmin Personal API anahtar dosyas\u0131') $script:GarminKey
        }
        $script:GarminKey = Resolve-UserPath $script:GarminKey
        $script:GarminPasted = ''
        if (-not (Test-Path -LiteralPath $script:GarminKey -PathType Leaf) -and -not $script:Unattended) {
            Say ((M '  {0} does not exist yet.' '  {0} hen\u00fcz yok.') -f $script:GarminKey)
            $script:GarminPasted = Read-Secret (M 'Paste your Garmin Personal API key (hidden; empty: skip Garmin)' 'Garmin Personal API anahtar\u0131n\u0131z\u0131 yap\u0131\u015ft\u0131r\u0131n (gizli; bo\u015f: Garmin atlan\u0131r)')
            if (-not $script:GarminPasted) {
                $script:InstallSources = @($script:InstallSources | Where-Object { $_ -ne 'garmin' })
                Add-Note (M 'Garmin was skipped (no key file). Re-run the installer with -Sources garmin once the key file exists.' 'Garmin atland\u0131 (anahtar dosyas\u0131 yok). Anahtar dosyas\u0131 haz\u0131r olunca kurulumu -Sources garmin ile yeniden \u00e7al\u0131\u015ft\u0131r\u0131n.')
            }
        }
        if ($script:InstallSources -contains 'garmin' -and -not $script:GarminPasted -and
                -not (Test-Path -LiteralPath $script:GarminKey -PathType Leaf)) {
            # No key file: continue with Garmin off (its mount would make `docker compose up` fail).
            Say-Warn ((M 'Garmin key file {0} does not exist; Garmin stays off.' 'Garmin anahtar dosyas\u0131 {0} yok; Garmin kapal\u0131 kal\u0131r.') -f $script:GarminKey)
            $script:InstallSources = @($script:InstallSources | Where-Object { $_ -ne 'garmin' })
            $script:GarminMissingKey = $script:GarminKey
            Add-Note ((M 'Garmin is off: the key file {0} does not exist yet. Save your Personal API key there (one line, readable only by you) and re-run the installer with -Sources garmin.' 'Garmin kapal\u0131: {0} anahtar dosyas\u0131 hen\u00fcz yok. Personal API anahtar\u0131n\u0131z\u0131 oraya kaydedin (tek sat\u0131r, yaln\u0131z siz okuyabilin) ve kurulumu -Sources garmin ile yeniden \u00e7al\u0131\u015ft\u0131r\u0131n.') -f $script:GarminKey)
        }
    }
    if ($script:InstallSources -contains 'apple' -and -not $AppleReceiver -and -not $script:Unattended) {
        Say ''
        Say (M 'Apple Health: export.zip uploads (System page) need nothing. The Health Auto Export receiver lets the iOS app push data itself; it needs an https address that reaches this machine.' 'Apple Sa\u011fl\u0131k: export.zip y\u00fckleme (Sistem sayfas\u0131) ayar istemez. Health Auto Export al\u0131c\u0131s\u0131 iOS uygulamas\u0131n\u0131n veriyi kendisinin g\u00f6ndermesini sa\u011flar; bu makineye ula\u015fan bir https adresi gerekir.')
        $script:AppleReceiverChoice = Read-YesNo (M 'Turn on the Health Auto Export receiver?' 'Health Auto Export al\u0131c\u0131s\u0131 a\u00e7\u0131ls\u0131n m\u0131?') 'no'
    }
    if ($script:AppleReceiverChoice -eq 'yes' -and -not $script:Unattended -and -not (Get-EnvValue 'APPLE_RECEIVER_ENDPOINT_URI')) {
        $script:AppleEndpoint = Read-Answer (M 'Receiver https URL ending in /webhooks/apple-health (empty: later)' 'Al\u0131c\u0131n\u0131n /webhooks/apple-health ile biten https adresi (bo\u015f: sonra)') ''
    }
    if ($script:InstallMode -eq 'full' -and -not $script:Unattended -and -not (Get-EnvValue 'GOOGLE_HEALTH_WEBHOOK_ENDPOINT_URI')) {
        $answer = Read-Answer (M 'Public webhook host name, e.g. webhook.example.com (empty: later)' 'Herkese a\u00e7\u0131k webhook ana makine ad\u0131, \u00f6rn. webhook.example.com (bo\u015f: sonra)') ''
        $script:WebhookHost = (($answer -replace '^https://', '') -split '/')[0]
    }
}

function Resolve-UserPath {
    param([string]$Path)
    if ($Path -eq '~') { $Path = $HOME }
    if ($Path.StartsWith('~/') -or $Path.StartsWith('~\')) { $Path = Join-Path $HOME $Path.Substring(2) }
    return [System.IO.Path]::GetFullPath($ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path))
}

function Select-HostJobs {
    $default = 'no'
    if ($script:InstallMode -eq 'full') { $default = 'yes' }
    if ($Backups) { $script:BackupsChoice = $Backups.ToLowerInvariant() } else {
        $script:BackupsChoice = Read-YesNo (M 'Daily encrypted backup at 03:30 (Task Scheduler)?' 'Her g\u00fcn 03:30''da \u015fifreli yedek (G\u00f6rev Zamanlay\u0131c\u0131s\u0131)?') $default
    }
    if ($Autostart) { $script:AutostartChoice = $Autostart.ToLowerInvariant() } else {
        $script:AutostartChoice = Read-YesNo (M 'Start Docker Desktop (and HealtOS) when you sign in?' 'Oturum a\u00e7\u0131l\u0131nca Docker Desktop (ve HealtOS) ba\u015flas\u0131n m\u0131?') 'yes'
    }
    if ($Umay) { $script:UmayChoice = $Umay.ToLowerInvariant() } else {
        $script:UmayChoice = Read-YesNo (M 'Umay bridge as a sign-in task (needs the Codex CLI signed in to ChatGPT)?' 'Umay k\u00f6pr\u00fcs\u00fc oturum g\u00f6revi olarak (ChatGPT''ye giri\u015f yapm\u0131\u015f Codex CLI gerekir)?') 'no'
    }
    # Explicit flags must be honoured or fail; defaults may be dropped with a warning.
    $script:BackupsExplicit = [bool]$Backups
    $script:UmayExplicit = [bool]$Umay
}

# ---------------------------------------------------------------------------------------------
# Preflight

function Get-FreeBytes {
    param([string]$Path)
    try {
        $root = [System.IO.Path]::GetPathRoot([System.IO.Path]::GetFullPath($Path))
        return [int64](New-Object System.IO.DriveInfo($root)).AvailableFreeSpace
    } catch {
        return $null
    }
}

function Invoke-Preflight {
    Say-Step (M 'Checking this computer' 'Bilgisayar denetleniyor')
    $version = [System.Environment]::OSVersion.Version
    if ((Test-IsWindowsHost) -and $version.Major -eq 10 -and $version.Build -lt 19045) {
        Say-Warn ((M 'Windows build {0}: Docker Desktop needs Windows 10 22H2 (build 19045) or Windows 11. Run Windows Update first.' 'Windows derlemesi {0}: Docker Desktop Windows 10 22H2 (derleme 19045) ya da Windows 11 ister. \u00d6nce Windows Update \u00e7al\u0131\u015ft\u0131r\u0131n.') -f $version.Build)
    }
    $free = Get-FreeBytes $script:RepoDir
    if ($null -ne $free) {
        $gib = [math]::Round($free / 1GB, 1)
        if ($free -lt 10GB) {
            Say-Warn ((M 'only {0} GiB free on the drive of {1}; images, the database and backups need 10 GiB or more. Free some space (Disk Cleanup) or choose another -Dir.' '{1} s\u00fcr\u00fcc\u00fcs\u00fcnde yaln\u0131z {0} GiB bo\u015f; imajlar, veritaban\u0131 ve yedekler i\u00e7in 10 GiB ya da daha fazlas\u0131 gerekir. Yer a\u00e7\u0131n (Disk Temizleme) ya da ba\u015fka bir -Dir se\u00e7in.') -f $gib, $script:RepoDir)
        } else {
            Say ((M '  Disk: {0} GiB free' '  Disk: {0} GiB bo\u015f') -f $gib)
        }
    }
    if (Find-Tool 'git') {
        Say (M '  git: found' '  git: var')
    } else {
        Say-Warn (M 'git was not found; updates (git pull) need it: winget install -e --id Git.Git' 'git bulunamad\u0131; g\u00fcncellemeler (git pull) i\u00e7in gerekli: winget install -e --id Git.Git')
    }
    if ($DryRun) {
        Say-Plan (M 'would check Docker Desktop (installed, running, WSL 2 backend, Compose v2) and start Docker Desktop if it is not running' 'Docker Desktop denetlenirdi (kurulu, \u00e7al\u0131\u015f\u0131yor, WSL 2 altyap\u0131s\u0131, Compose v2) ve \u00e7al\u0131\u015fm\u0131yorsa ba\u015flat\u0131l\u0131rd\u0131')
    } else {
        Test-Docker
    }
}

function Test-HostJobTools {
    # Tools the chosen host jobs need. An explicit -Backups yes / -Umay yes stops with exit 2
    # when they are missing; a default is dropped with a warning.
    if ($script:BackupsChoice -eq 'yes' -and (-not (Find-Tool 'age') -or -not (Find-Tool 'age-keygen'))) {
        $hint = M 'age is needed for encrypted backups: winget install -e --id FiloSottile.age (then open a new PowerShell window)' '\u015eifreli yedekler i\u00e7in age gerekli: winget install -e --id FiloSottile.age (sonra yeni bir PowerShell penceresi a\u00e7\u0131n)'
        if ($DryRun) {
            Say-Warn $hint
        } elseif ($script:BackupsExplicit) {
            Stop-Installer 2 $hint
        } else {
            Say-Warn $hint
            Add-Note (M 'Backups were skipped (age is not installed). Install age and re-run the installer with -Backups yes.' 'Yedekler atland\u0131 (age kurulu de\u011fil). age''i kurup kurulumu -Backups yes ile yeniden \u00e7al\u0131\u015ft\u0131r\u0131n.')
            $script:BackupsChoice = 'no'
        }
    }
    if ($script:UmayChoice -eq 'yes') {
        $missing = @()
        foreach ($tool in @('codex', 'node', 'uv')) { if (-not (Find-Tool $tool)) { $missing += $tool } }
        if ($missing.Count -gt 0) {
            $hint = (M 'The Umay bridge needs {0}: winget install -e --id OpenJS.NodeJS.LTS; npm install -g @openai/codex; codex login; winget install -e --id astral-sh.uv (see docs/legacy/WINDOWS.md).' 'Umay k\u00f6pr\u00fcs\u00fc i\u00e7in {0} gerekli: winget install -e --id OpenJS.NodeJS.LTS; npm install -g @openai/codex; codex login; winget install -e --id astral-sh.uv (docs/legacy/WINDOWS.md''ye bak\u0131n).') -f ($missing -join ', ')
            if ($DryRun) {
                Say-Warn $hint
            } elseif ($script:UmayExplicit) {
                Stop-Installer 2 $hint
            } else {
                Say-Warn $hint
                Add-Note (M 'The Umay bridge was skipped (tools missing, see above).' 'Umay k\u00f6pr\u00fcs\u00fc atland\u0131 (ara\u00e7lar eksik, yukar\u0131ya bak\u0131n).')
                $script:UmayChoice = 'no'
            }
        }
    }
}

function Find-DockerDesktopExe {
    foreach ($base in @($env:ProgramFiles, $env:ProgramW6432)) {
        if (-not $base) { continue }
        $candidate = Join-Path $base 'Docker\Docker\Docker Desktop.exe'
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    }
    return $null
}

function Test-DockerEngine {
    $docker = Find-Tool 'docker'
    if (-not $docker) { return $false }
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $docker info --format '{{.ServerVersion}}' 2>$null | Out-Null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    } finally {
        $ErrorActionPreference = $previous
    }
}

function Test-Docker {
    $desktop = Find-DockerDesktopExe
    $docker = Find-Tool 'docker'
    if (-not $docker -and -not $desktop) {
        Stop-Installer 2 (M 'Docker Desktop is not installed. Install it with: winget install -e --id Docker.DockerDesktop (needs administrator rights and usually a restart), start it once, keep "Use the WSL 2 based engine", then run the installer again in a new PowerShell window.' 'Docker Desktop kurulu de\u011fil. \u015eununla kurun: winget install -e --id Docker.DockerDesktop (y\u00f6netici izni ve genellikle yeniden ba\u015flatma gerekir), bir kez ba\u015flat\u0131n, "Use the WSL 2 based engine" se\u00e7ili kals\u0131n, sonra kurulumu yeni bir PowerShell penceresinde yeniden \u00e7al\u0131\u015ft\u0131r\u0131n.')
    }
    $wsl = Find-Tool 'wsl'
    $wslOk = $false
    if ($wsl) {
        $previous = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try { & $wsl --status 2>$null | Out-Null; $wslOk = ($LASTEXITCODE -eq 0) } catch { $wslOk = $false }
        $ErrorActionPreference = $previous
    }
    if (-not $wslOk) {
        Say-Warn (M 'WSL 2 does not answer (wsl --status). Docker Desktop needs it: run "wsl --install" (or "wsl --update") as administrator and restart Windows.' 'WSL 2 yan\u0131t vermiyor (wsl --status). Docker Desktop i\u00e7in gerekli: y\u00f6netici olarak "wsl --install" (ya da "wsl --update") \u00e7al\u0131\u015ft\u0131r\u0131p Windows''u yeniden ba\u015flat\u0131n.')
    }
    if (-not (Test-DockerEngine)) {
        if ($desktop) {
            Say (M 'Docker Desktop is not running; starting it (this can take a minute)...' 'Docker Desktop \u00e7al\u0131\u015fm\u0131yor; ba\u015flat\u0131l\u0131yor (bir dakika s\u00fcrebilir)...')
            Start-Process -FilePath $desktop | Out-Null
            for ($i = 0; $i -lt 60 -and -not (Test-DockerEngine); $i++) { Start-Sleep -Seconds 5 }
        }
        if (-not (Test-DockerEngine)) {
            Stop-Installer 2 (M 'The Docker engine is not running. Start Docker Desktop, wait until it says "Engine running", then run the installer again. If Docker Desktop reports a WSL error: wsl --update (administrator) and restart.' 'Docker motoru \u00e7al\u0131\u015fm\u0131yor. Docker Desktop''u ba\u015flat\u0131n, "Engine running" yaz\u0131s\u0131n\u0131 bekleyin, sonra kurulumu yeniden \u00e7al\u0131\u015ft\u0131r\u0131n. Docker Desktop WSL hatas\u0131 verirse: wsl --update (y\u00f6netici) ve yeniden ba\u015flatma.')
        }
    }
    $docker = Find-Tool 'docker'
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $kernel = (& $docker info --format '{{.KernelVersion}}' 2>$null | Out-String).Trim()
        & $docker compose version 2>$null | Out-Null
        $composeOk = ($LASTEXITCODE -eq 0)
    } finally {
        $ErrorActionPreference = $previous
    }
    if (-not $composeOk) {
        Stop-Installer 2 (M 'docker compose (Compose v2) is missing; update Docker Desktop.' 'docker compose (Compose v2) yok; Docker Desktop''u g\u00fcncelleyin.')
    }
    if ($kernel -and $kernel -notmatch 'WSL2') {
        Say-Warn ((M 'Docker reports kernel {0}; the WSL 2 backend is recommended (Docker Desktop > Settings > General).' 'Docker \u00e7ekirde\u011fi {0}; WSL 2 altyap\u0131s\u0131 \u00f6nerilir (Docker Desktop > Settings > General).') -f $kernel)
    }
    Say (M 'Docker Desktop: running, Compose v2 available' 'Docker Desktop: \u00e7al\u0131\u015f\u0131yor, Compose v2 var')
}

# ---------------------------------------------------------------------------------------------
# .env

function New-RandomBytes {
    param([int]$Count)
    $bytes = New-Object byte[] $Count
    $generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $generator.GetBytes($bytes) } finally { $generator.Dispose() }
    return , $bytes
}

function New-HexSecret {
    return -join ((New-RandomBytes 32) | ForEach-Object { $_.ToString('x2') })
}

function New-UrlSafeSecret {
    # Like Python's secrets.token_urlsafe(32): 43 characters, no padding.
    return ([System.Convert]::ToBase64String((New-RandomBytes 32))).Replace('+', '-').Replace('/', '_').TrimEnd('=')
}

function New-FernetKey {
    # Fernet.generate_key(): urlsafe base64 of 32 random bytes, with padding (44 characters).
    return ([System.Convert]::ToBase64String((New-RandomBytes 32))).Replace('+', '-').Replace('/', '_')
}

function Read-EnvLines {
    $envFile = Join-Path $script:RepoDir '.env'
    $script:EnvExisted = Test-Path -LiteralPath $envFile -PathType Leaf
    $source = Join-Path $script:RepoDir '.env.example'
    if ($script:EnvExisted) { $source = $envFile }
    $text = [System.IO.File]::ReadAllText($source)
    $lines = New-Object System.Collections.Generic.List[string]
    foreach ($line in ($text -replace "`r", '').Split("`n")) { $lines.Add($line) }
    while ($lines.Count -gt 0 -and $lines[$lines.Count - 1] -eq '') { $lines.RemoveAt($lines.Count - 1) }
    $script:EnvLines = $lines
    $script:EnvSet = New-Object System.Collections.Generic.List[string]
    $script:EnvAppendedHeader = $false
}

function Find-EnvLine {
    # `export KEY=value` counts as KEY too (Compose reads it), so a key is never added twice.
    param([string]$Name)
    $pattern = '^\s*(export\s+)?' + [regex]::Escape($Name) + '\s*='
    for ($i = $script:EnvLines.Count - 1; $i -ge 0; $i--) {
        if ($script:EnvLines[$i] -match $pattern) { return $i }
    }
    return -1
}

function Get-EnvValue {
    # Last plain KEY=value, one pair of quotes removed, never evaluated (same rule as backup.sh).
    param([string]$Name)
    if ($null -eq $script:EnvLines) { return '' }
    $index = Find-EnvLine $Name
    if ($index -lt 0) { return '' }
    $value = ($script:EnvLines[$index] -replace ('^\s*(export\s+)?' + [regex]::Escape($Name) + '\s*=\s*'), '').Trim()
    $value = $value -replace '^["'']', ''
    $value = $value -replace '["'']$', ''
    return $value
}

function Format-EnvValue {
    # Values with spaces, backslashes or # are single-quoted (Compose reads them literally).
    param([string]$Value)
    if ($Value -match '[\s\\#"]' -and $Value -notmatch "'") { return "'" + $Value + "'" }
    return $Value
}

function Write-EnvKey {
    # Replaces the last assignment of the key, or appends it under one header.
    param([string]$Name, [string]$Value)
    $line = $Name + '=' + (Format-EnvValue $Value)
    $index = Find-EnvLine $Name
    if ($index -ge 0) {
        # An `export ` prefix is kept.
        if ($script:EnvLines[$index] -match '^\s*export\s+') { $line = 'export ' + $line }
        $script:EnvLines[$index] = $line
    } else {
        if (-not $script:EnvAppendedHeader) {
            $stamp = [System.DateTime]::UtcNow.ToString("yyyy'-'MM'-'dd'T'HH':'mm':'ss'Z'", [System.Globalization.CultureInfo]::InvariantCulture)
            $script:EnvLines.Add('')
            $script:EnvLines.Add("# Added by install.ps1 $stamp")
            $script:EnvAppendedHeader = $true
        }
        $script:EnvLines.Add($line)
    }
    if ($script:EnvSet -notcontains $Name) { $script:EnvSet.Add($Name) }
}

function Set-EnvSecret {
    # A secret: generated only when missing or empty; an existing one is never rotated.
    param([string]$Name, [string]$Value)
    if (-not (Get-EnvValue $Name)) { Write-EnvKey $Name $Value }
}

function Set-EnvManaged {
    # Installer-managed keys (COMPOSE_FILE and its separator) follow the chosen mode and
    # sources; the old value stays in .env.bak-<stamp>.
    param([string]$Name, [string]$Value)
    $current = Get-EnvValue $Name
    if ($current -cne $Value) {
        Write-EnvKey $Name $Value
        if ($current -and $script:EnvExisted) {
            Add-Note ((M '{0} changed from ''{1}'' to ''{2}'' (the old .env is kept as .env.bak-<stamp>).' '{0} ''{1}'' de\u011ferinden ''{2}'' de\u011ferine de\u011fi\u015fti (eski .env, .env.bak-<damga> olarak sakl\u0131).') -f $Name, $current, $Value)
        }
    }
}

function Test-EnvOn {
    # An enable flag is off when empty or false/0/no/off (any case); anything else is left as
    # it is and counts as on. The same rule as install.sh.
    param([string]$Value)
    $text = ([string]$Value).Trim().ToLowerInvariant()
    return -not ($text -eq '' -or @('false', '0', 'no', 'off') -contains $text)
}

function Set-EnvEnable {
    # Turns a source on (empty, false, 0, no or off -> true); never turns anything off.
    param([string]$Name)
    if (-not (Test-EnvOn (Get-EnvValue $Name))) { Write-EnvKey $Name 'true' }
}

function Set-EnvGiven {
    # A value you typed: filled when empty; never replaces an existing one.
    param([string]$Name, [string]$Value)
    if (-not $Value) { return }
    $current = Get-EnvValue $Name
    if (-not $current) {
        Write-EnvKey $Name $Value
    } elseif ($current -cne $Value) {
        Add-Note ((M '{0} already has a value in .env; the new one was not written.' '{0} i\u00e7in .env i\u00e7inde zaten bir de\u011fer var; yenisi yaz\u0131lmad\u0131.') -f $Name)
    }
}

function Get-RedirectUri {
    $uri = Get-EnvValue 'GOOGLE_REDIRECT_URI'
    if ($uri) { return $uri }
    return "http://localhost:$(Get-AppPort)/auth/google/callback"
}

function Get-AppPort {
    $port = Get-EnvValue 'APP_PORT'
    if ($port -match '^[0-9]+$') { return $port }
    return '8000'
}

function Get-ComposeEntries {
    # The existing COMPOSE_FILE as a list, split with the existing COMPOSE_PATH_SEPARATOR
    # (';', the Windows default, when none is set).
    $separator = Get-EnvValue 'COMPOSE_PATH_SEPARATOR'
    if (-not $separator) { $separator = ';' }
    $entries = @()
    foreach ($entry in (Get-EnvValue 'COMPOSE_FILE').Split($separator)) {
        if ($entry.Trim() -ne '') { $entries += $entry.Trim() }
    }
    return , $entries
}

function ConvertTo-ComposeName {
    # ./docker-compose.yml and .\docker-compose.yml name the same file as docker-compose.yml.
    param([string]$Entry)
    return ($Entry -replace '^(\.[\\/])+', '')
}

function Get-ComposeFiles {
    # The files the installer manages (base file, the trial override, each source mount whose
    # ..._ENABLED is on and whose host path is set, so `up` cannot fail on a missing mount),
    # then every other file of the existing COMPOSE_FILE in its original order (kept, never
    # dropped). Without a COMPOSE_FILE, Compose loaded docker-compose.override.yml by itself, so
    # it is kept as well. Joined with ';' (Windows separator).
    $files = @('docker-compose.yml')
    if ($script:InstallMode -ne 'full') { $files += 'docker-compose.dev.yml' }
    if ((Test-EnvOn (Get-EnvValue 'GARMIN_ENABLED')) -and (Get-EnvValue 'GARMIN_API_KEY_HOST_FILE')) {
        $files += 'docker-compose.garmin.yml'
    }
    if ((Test-EnvOn (Get-EnvValue 'APPLE_RECEIVER_ENABLED')) -and (Get-EnvValue 'APPLE_RECEIVER_TOKEN_HOST_FILE')) {
        $files += 'docker-compose.apple.yml'
    }
    $existing = Get-ComposeEntries
    if ($existing.Count -eq 0) {
        if (Test-Path -LiteralPath (Join-Path $script:RepoDir 'docker-compose.override.yml') -PathType Leaf) {
            $existing = @('docker-compose.override.yml')
        }
    }
    foreach ($file in $existing) {
        if ($HealtOSManagedCompose -notcontains (ConvertTo-ComposeName $file) -and $files -notcontains $file) {
            $files += $file
        }
    }
    return ($files -join ';')
}

function Update-EnvSettings {
    Read-EnvLines
    Set-EnvSecret 'POSTGRES_PASSWORD' (New-HexSecret)
    Set-EnvSecret 'APP_UI_TOKEN' (New-UrlSafeSecret)
    if ($script:InstallSources -contains 'google' -or $script:InstallMode -eq 'full') {
        Set-EnvSecret 'GOOGLE_TOKEN_ENCRYPTION_KEY' (New-FernetKey)
        Set-EnvGiven 'GOOGLE_CLIENT_ID' $script:GoogleId
        Set-EnvGiven 'GOOGLE_CLIENT_SECRET' $script:GoogleSecret
    }
    if ($script:InstallMode -eq 'full') {
        Set-EnvSecret 'GOOGLE_HEALTH_WEBHOOK_AUTHORIZATION' ('Bearer ' + (New-UrlSafeSecret))
        if ($script:WebhookHost) {
            Set-EnvGiven 'GOOGLE_HEALTH_WEBHOOK_ENDPOINT_URI' "https://$($script:WebhookHost)/webhooks/google-health"
        }
    }
    if ($script:InstallSources -contains 'garmin') {
        if ($GarminKeyFile) {
            # An explicit -GarminKeyFile replaces the path (the old .env is copied first).
            Set-EnvManaged 'GARMIN_API_KEY_HOST_FILE' $script:GarminKey
        } else {
            Set-EnvGiven 'GARMIN_API_KEY_HOST_FILE' $script:GarminKey
        }
        Set-EnvEnable 'GARMIN_ENABLED'
    }
    if ($script:AppleReceiverChoice -eq 'yes') {
        Set-EnvGiven 'APPLE_RECEIVER_TOKEN_HOST_FILE' (Get-AppleTokenPath)
        Set-EnvEnable 'APPLE_RECEIVER_ENABLED'
        Set-EnvGiven 'APPLE_RECEIVER_ENDPOINT_URI' $script:AppleEndpoint
    }
    if ($script:UmayChoice -eq 'yes') {
        # Written here once (only when missing or empty), never by the bridge task: the task
        # reads them and never rewrites .env.
        Set-EnvSecret 'CODEX_BRIDGE_TOKEN' (New-UrlSafeSecret)
        Set-EnvSecret 'CODEX_BRIDGE_URL' 'ws://host.docker.internal:8766'
    }
    # Read the old list with the old separator before the separator becomes ';'.
    $composeFiles = Get-ComposeFiles
    Set-EnvManaged 'COMPOSE_PATH_SEPARATOR' ';'
    Set-EnvManaged 'COMPOSE_FILE' $composeFiles
}

function Write-EnvFile {
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    $text = (($script:EnvLines.ToArray()) -join "`n") + "`n"
    $envFile = Join-Path $script:RepoDir '.env'
    Say-Step (M 'Configuration (.env)' 'Yap\u0131land\u0131rma (.env)')
    if ($DryRun) {
        $out = $EnvOut
        if (-not $out) { $out = Join-Path $script:RepoDir '.env.dry-run' }
        $out = Resolve-UserPath $out
        if ($out -eq $envFile) { Stop-Installer 2 (M '-EnvOut must not be the real .env.' '-EnvOut ger\u00e7ek .env olamaz.') }
        $marker = '# install.ps1 -DryRun: the .env it would write. Not used by Docker.'
        if (Test-Path -LiteralPath $out) {
            $first = ''
            try { $first = ([System.IO.File]::ReadAllLines($out) | Select-Object -First 1) } catch { $first = '' }
            if ($first -ne $marker) {
                Stop-Installer 2 ((M '{0} exists and is not an earlier dry-run preview; it is not overwritten. Choose another -EnvOut.' '{0} var ve \u00f6nceki bir deneme \u00f6nizlemesi de\u011fil; \u00fczerine yaz\u0131lmaz. Ba\u015fka bir -EnvOut se\u00e7in.') -f $out)
            }
        }
        if (-not $script:EnvExisted) {
            Say-Plan ((M 'would create {0} from .env.example (readable only by you)' '.env.example''dan {0} olu\u015fturulurdu (yaln\u0131z siz okuyabilirsiniz)') -f $envFile)
        } elseif ($script:EnvSet.Count -gt 0) {
            Say-Plan (M 'would copy .env to .env.bak-<UTC stamp> (readable only by you), then fill only missing or empty keys' '.env, .env.bak-<UTC damga> olarak kopyalan\u0131r (yaln\u0131z siz okuyabilirsiniz), sonra yaln\u0131z eksik veya bo\u015f anahtarlar doldurulurdu')
        }
        if ($script:EnvExisted) { Protect-EnvFiles }
        # The preview holds the same secrets: it is readable only by you as well.
        Write-PrivateBytes -Path $out -Bytes $utf8.GetBytes($marker + "`n" + $text) -Existing
        $script:EnvResult = $out
        Say ((M '  Preview written to {0} (readable only by you; the real .env is untouched).' '  \u00d6nizleme {0} dosyas\u0131na yaz\u0131ld\u0131 (yaln\u0131z siz okuyabilirsiniz; ger\u00e7ek .env''e dokunulmad\u0131).') -f $out)
        return
    }
    if (-not $script:EnvExisted) {
        $temporary = Join-Path $script:RepoDir ('.env.install-' + [System.IO.Path]::GetRandomFileName().Replace('.', ''))
        Write-PrivateBytes -Path $temporary -Bytes $utf8.GetBytes($text)
        [System.IO.File]::Move($temporary, $envFile)
        Say (M '  Created .env from .env.example (readable only by you).' '  .env, .env.example''dan olu\u015fturuldu (yaln\u0131z siz okuyabilirsiniz).')
    } elseif ($script:EnvSet.Count -gt 0) {
        $stamp = [System.DateTime]::UtcNow.ToString("yyyyMMdd'T'HHmmss'Z'", [System.Globalization.CultureInfo]::InvariantCulture)
        $backup = "$envFile.bak-$stamp"
        $n = 1
        while (Test-Path -LiteralPath $backup) { $n++; $backup = "$envFile.bak-$stamp-$n" }
        # A byte-for-byte copy, created new (never over an existing file), readable only by you.
        Write-PrivateBytes -Path $backup -Bytes ([System.IO.File]::ReadAllBytes($envFile))
        $script:EnvBackup = $backup
        Say ((M '  Copied the existing .env to {0} (kept forever, readable only by you).' '  Var olan .env {0} olarak kopyaland\u0131 (silinmez, yaln\u0131z siz okuyabilirsiniz).') -f $backup)
        $temporary = Join-Path $script:RepoDir ('.env.install-' + [System.IO.Path]::GetRandomFileName().Replace('.', ''))
        Write-PrivateBytes -Path $temporary -Bytes $utf8.GetBytes($text)
        [System.IO.File]::Replace($temporary, $envFile, [NullString]::Value)
        # Replace keeps the old file's permissions: narrow them again.
        Set-HealtOSPrivateAcl -Path $envFile
        Say (M '  Filled only missing or empty keys; existing values were not changed.' '  Yaln\u0131z eksik veya bo\u015f anahtarlar dolduruldu; var olan de\u011ferler de\u011fi\u015ftirilmedi.')
    } else {
        Say (M '  .env already complete; not modified.' '  .env zaten tam; de\u011fi\u015ftirilmedi.')
    }
    Protect-EnvFiles
    if ($script:EnvSet.Count -gt 0) {
        Say ((M '  Keys set: {0}' '  Ayarlanan anahtarlar: {0}') -f ($script:EnvSet -join ' '))
    }
}

function Protect-EnvFiles {
    # .env and every .env.bak-* hold the secrets: readable only by you (content unchanged).
    $files = @(Join-Path $script:RepoDir '.env')
    $files += @(Get-ChildItem -LiteralPath $script:RepoDir -Filter '.env.bak-*' -Force -File -ErrorAction SilentlyContinue |
        ForEach-Object { $_.FullName })
    foreach ($file in $files) { Protect-SecretFile $file }
}

function Get-MissingProduction {
    # What the app (production mode, no development override) or a chosen override needs.
    $missing = New-Object System.Collections.Generic.List[string]
    $files = @((Get-EnvValue 'COMPOSE_FILE') -split '[;:]')
    if ($files -notcontains 'docker-compose.dev.yml') {
        foreach ($name in @('APP_UI_TOKEN', 'GOOGLE_CLIENT_ID', 'GOOGLE_CLIENT_SECRET', 'GOOGLE_TOKEN_ENCRYPTION_KEY',
                'GOOGLE_HEALTH_WEBHOOK_AUTHORIZATION')) {
            if (-not (Get-EnvValue $name)) { $missing.Add(((M '{0} is empty' '{0} bo\u015f') -f $name)) }
        }
        if (-not (Get-EnvValue 'GOOGLE_HEALTH_WEBHOOK_ENDPOINT_URI').StartsWith('https://')) {
            $missing.Add((M 'GOOGLE_HEALTH_WEBHOOK_ENDPOINT_URI must be https://<your webhook host>/webhooks/google-health' 'GOOGLE_HEALTH_WEBHOOK_ENDPOINT_URI https://<webhook-adresiniz>/webhooks/google-health olmal\u0131'))
        }
    }
    if ($files -contains 'docker-compose.garmin.yml') {
        $key = Get-EnvValue 'GARMIN_API_KEY_HOST_FILE'
        if (-not $key -or -not (Test-Path -LiteralPath $key -PathType Leaf)) {
            if (-not ($DryRun -and $script:GarminPasted)) {
                $missing.Add(((M 'Garmin key file missing: {0}' 'Garmin anahtar dosyas\u0131 yok: {0}') -f $key))
            }
        }
    }
    return , $missing.ToArray()
}

# ---------------------------------------------------------------------------------------------
# Secrets outside .env: backup key, Apple receiver token

function Get-ConfigDir {
    if ((Test-IsWindowsHost) -and $env:APPDATA) { return (Join-Path $env:APPDATA 'HealtOS') }
    return (Join-Path (Join-Path $HOME '.config') 'healtos')
}

function Get-UserHome {
    if ($env:USERPROFILE) { return $env:USERPROFILE }
    return $HOME
}

function Initialize-PrivateDirectory {
    param([string]$Path)
    if (Test-Path -LiteralPath $Path -PathType Container) { return }
    New-Item -ItemType Directory -Path $Path -Force | Out-Null
    Set-HealtOSPrivateAcl -Path $Path -Directory
}

function Write-PrivateBytes {
    # Writes a file readable only by you. The file is created empty, narrowed to your account
    # and only then filled, so its content is never readable by anyone else, not even briefly.
    # CreateNew: an existing file is never replaced; -Existing allows an earlier file at $Path
    # (the dry-run preview).
    param([string]$Path, [byte[]]$Bytes, [switch]$Existing)
    $mode = [System.IO.FileMode]::CreateNew
    if ($Existing) { $mode = [System.IO.FileMode]::OpenOrCreate }
    $stream = New-Object System.IO.FileStream($Path, $mode, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
    $stream.Dispose()
    Set-HealtOSPrivateAcl -Path $Path
    $stream = New-Object System.IO.FileStream($Path, [System.IO.FileMode]::Truncate,
        [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
    try {
        $stream.Write($Bytes, 0, $Bytes.Length)
    } finally {
        $stream.Dispose()
    }
}

function New-PrivateFile {
    # Creates a new file readable only by you; never replaces an existing one.
    param([string]$Path, [string]$Content)
    $directory = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $directory -PathType Container)) { Initialize-PrivateDirectory $directory }
    Write-PrivateBytes -Path $Path -Bytes ((New-Object System.Text.UTF8Encoding($false)).GetBytes($Content))
}

function Protect-SecretFile {
    # An existing key or token file readable by others is narrowed to your account only (its
    # content is never touched).
    param([string]$Path)
    if (-not $Path -or -not (Test-Path -LiteralPath $Path -PathType Leaf)) { return }
    $problem = Get-HealtOSAclProblem $Path
    if (-not $problem) { return }
    if ($DryRun) {
        Say-Plan ((M 'would make {0} readable only by you (it is {1})' '{0} yaln\u0131z sizin okuyabilece\u011finiz hale getirilirdi (\u015fu an: {1})') -f $Path, $problem)
        return
    }
    Set-HealtOSPrivateAcl -Path $Path
    Say ((M '  {0} is now readable only by you (the content is unchanged).' '  {0} art\u0131k yaln\u0131z sizin taraf\u0131n\u0131zdan okunabilir (i\u00e7erik de\u011fi\u015fmedi).') -f $Path)
}

function Save-GarminKey {
    if (-not ($script:InstallSources -contains 'garmin')) { return }
    if (-not $script:GarminPasted) {
        Protect-SecretFile $script:GarminKey
        return
    }
    if ($DryRun) {
        Say-Plan ((M 'would save the key to {0} (readable only by you)' 'anahtar {0} dosyas\u0131na kaydedilirdi (yaln\u0131z siz okuyabilirsiniz)') -f $script:GarminKey)
        return
    }
    New-PrivateFile -Path $script:GarminKey -Content ($script:GarminPasted + "`n")
    Say ((M '  Saved the Garmin key to {0} (readable only by you).' '  Garmin anahtar\u0131 {0} dosyas\u0131na kaydedildi (yaln\u0131z siz okuyabilirsiniz).') -f $script:GarminKey)
}

function Get-AppleTokenPath {
    $current = Get-EnvValue 'APPLE_RECEIVER_TOKEN_HOST_FILE'
    if ($current) { return (Resolve-UserPath $current) }
    return (Join-Path (Join-Path (Join-Path (Get-UserHome) '.config') 'healthos') 'apple-receiver-token')
}

function Initialize-AppleToken {
    # Same file format as `health-mirror apple-token`: token_urlsafe(32) and a newline.
    if ($script:AppleReceiverChoice -ne 'yes') { return }
    $file = Get-EnvValue 'APPLE_RECEIVER_TOKEN_HOST_FILE'
    if (-not $file) { return }
    $script:AppleTokenFile = $file
    if (Test-Path -LiteralPath $file -PathType Leaf) {
        Say ((M '  Apple receiver token file exists; kept (never rotated): {0}' '  Apple al\u0131c\u0131 belirte\u00e7 dosyas\u0131 var; korundu (yenilenmedi): {0}') -f $file)
        Protect-SecretFile $file
        return
    }
    if ($DryRun) {
        Say-Plan ((M 'would create the receiver token {0} (43 characters, like ''health-mirror apple-token'')' 'al\u0131c\u0131 belirteci {0} olu\u015fturulurdu (43 karakter, ''health-mirror apple-token'' gibi)') -f $file)
        return
    }
    $token = New-UrlSafeSecret
    New-PrivateFile -Path $file -Content ($token + "`n")
    $script:AppleTokenNew = $token
    Say ((M '  Created the Apple receiver token file {0}.' '  Apple al\u0131c\u0131 belirte\u00e7 dosyas\u0131 {0} olu\u015fturuldu.') -f $file)
}

function Initialize-BackupKey {
    # An age identity outside the checkout and outside .env, readable only by you; never
    # overwritten.
    $script:BackupKey = Join-Path (Get-ConfigDir) 'backup.key'
    $keygen = Find-Tool 'age-keygen'
    if (Test-Path -LiteralPath $script:BackupKey -PathType Leaf) {
        Say ((M '  Existing backup key kept: {0}' '  Mevcut yedek anahtar\u0131 korundu: {0}') -f $script:BackupKey)
        Protect-SecretFile $script:BackupKey
    } elseif ($DryRun) {
        Say-Plan ('age-keygen -o "{0}"  ' -f $script:BackupKey + (M '(readable only by you)' '(yaln\u0131z siz okuyabilirsiniz)'))
        return
    } else {
        Initialize-PrivateDirectory (Get-ConfigDir)
        $previous = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try { & $keygen -o $script:BackupKey 2>$null } finally { $ErrorActionPreference = $previous }
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $script:BackupKey -PathType Leaf)) {
            Stop-Installer 1 ((M 'Could not create the backup key {0}.' 'Yedek anahtar\u0131 {0} olu\u015fturulamad\u0131.') -f $script:BackupKey)
        }
        Set-HealtOSPrivateAcl -Path $script:BackupKey
        Say ((M '  Created the backup key {0} (readable only by you).' '  Yedek anahtar\u0131 {0} olu\u015fturuldu (yaln\u0131z siz okuyabilirsiniz).') -f $script:BackupKey)
    }
    if ($keygen -and -not $DryRun) {
        $previous = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try { $script:BackupPubKey = (& $keygen -y $script:BackupKey 2>$null | Out-String).Trim() } finally { $ErrorActionPreference = $previous }
    }
}

# ---------------------------------------------------------------------------------------------
# Host jobs and start

function Invoke-ChildScript {
    param([string]$Path, [hashtable]$Parameters)
    & $Path @Parameters | Out-Host
    return $LASTEXITCODE
}

function Install-HostJobs {
    $windowsDir = Join-Path (Join-Path $script:RepoDir 'deploy') 'windows'
    if ($script:AutostartChoice -eq 'yes') {
        Say-Step (M 'Autostart' 'Otomatik ba\u015flatma')
        $code = Invoke-ChildScript (Join-Path (Join-Path $script:RepoDir 'scripts') 'enable-docker-desktop-autostart.ps1') @{ DryRun = [bool]$DryRun }
        if ($code -ne 0) { Say-Warn (M 'Docker Desktop autostart was not configured.' 'Docker Desktop otomatik ba\u015flatmas\u0131 ayarlanamad\u0131.') }
    }
    if ($script:BackupsChoice -eq 'yes') {
        Say-Step (M 'Backup key' 'Yedek anahtar\u0131')
        Initialize-BackupKey
    }
    if ($script:BackupsChoice -eq 'yes' -or $script:UmayChoice -eq 'yes') {
        Say-Step (M 'Scheduled tasks' 'Zamanlanm\u0131\u015f g\u00f6revler')
        $parameters = @{
            Backup = ($script:BackupsChoice -eq 'yes')
            Umay = ($script:UmayChoice -eq 'yes')
            DryRun = [bool]$DryRun
            RepoDir = $script:RepoDir
        }
        $code = Invoke-ChildScript (Join-Path $windowsDir 'register-tasks.ps1') $parameters
        if ($code -ne 0) { Stop-Installer 1 (M 'Registering the scheduled tasks failed.' 'Zamanlanm\u0131\u015f g\u00f6revler kaydedilemedi.') }
    }
    if ($script:UmayChoice -eq 'yes') {
        Say-Step (M 'Umay bridge' 'Umay k\u00f6pr\u00fcs\u00fc')
        if ($DryRun) {
            Say-Plan 'uv sync --frozen --no-dev'
            Say-Plan ((M 'start the bridge now: {0}' 'k\u00f6pr\u00fc \u015fimdi ba\u015flat\u0131l\u0131r: {0}') -f (Join-Path $windowsDir 'start-umay-bridge.ps1'))
        } else {
            $uv = Find-Tool 'uv'
            Push-Location $script:RepoDir
            # Runtime dependencies only, like install.sh (the bridge needs no development tools).
            try { & $uv sync --frozen --no-dev | Out-Host } finally { Pop-Location }
            if ($LASTEXITCODE -ne 0) { Say-Warn (M 'uv sync failed; the bridge task will retry at the next sign-in.' 'uv sync ba\u015far\u0131s\u0131z; k\u00f6pr\u00fc g\u00f6revi bir sonraki oturumda yeniden dener.') }
            $code = Invoke-ChildScript (Join-Path $windowsDir 'start-umay-bridge.ps1') @{}
            if ($code -ne 0) {
                Say-Warn ((M 'The bridge did not start (is "codex login" done?). Log: {0}' 'K\u00f6pr\u00fc ba\u015flamad\u0131 ("codex login" yap\u0131ld\u0131 m\u0131?). G\u00fcnl\u00fck: {0}') -f (Join-Path (Get-HealtOSLogDir) 'umay-bridge.log'))
            }
        }
    }
}

function Start-Stack {
    Say-Step (M 'Starting HealtOS' 'HealtOS ba\u015flat\u0131l\u0131yor')
    if ($DryRun) {
        Say-Plan ((M 'docker compose up -d --build (in {0}; COMPOSE_FILE from .env)' 'docker compose up -d --build ({0} i\u00e7inde; COMPOSE_FILE .env''den)') -f $script:RepoDir)
        Say-Plan ((M 'wait for http://127.0.0.1:{0}/health/live' 'http://127.0.0.1:{0}/health/live beklenir') -f (Get-AppPort))
        return $true
    }
    $docker = Find-Tool 'docker'
    # The host owns the backup metadata directory; create it before Compose would.
    $statusDir = Get-EnvValue 'BACKUP_STATUS_HOST_DIR'
    if (-not $statusDir) { $statusDir = './backup-status' }
    $statusDir = Resolve-HealtOSPath -Path $statusDir -Base $script:RepoDir
    if (-not (Test-Path -LiteralPath $statusDir -PathType Container)) { New-Item -ItemType Directory -Path $statusDir -Force | Out-Null }
    Say (M 'The first build downloads and builds the images; this takes a few minutes.' '\u0130lk derleme imajlar\u0131 indirip olu\u015fturur; birka\u00e7 dakika s\u00fcrer.')
    Push-Location $script:RepoDir
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    # Compose must read COMPOSE_FILE from .env; a value in this shell would override it.
    $savedFile = $env:COMPOSE_FILE
    $savedSeparator = $env:COMPOSE_PATH_SEPARATOR
    [System.Environment]::SetEnvironmentVariable('COMPOSE_FILE', $null, 'Process')
    [System.Environment]::SetEnvironmentVariable('COMPOSE_PATH_SEPARATOR', $null, 'Process')
    try {
        & $docker compose up -d --build | Out-Host
        $code = $LASTEXITCODE
    } finally {
        if ($savedFile) { $env:COMPOSE_FILE = $savedFile }
        if ($savedSeparator) { $env:COMPOSE_PATH_SEPARATOR = $savedSeparator }
        $ErrorActionPreference = $previous
        Pop-Location
    }
    if ($code -ne 0) {
        Stop-Installer 1 (M 'docker compose up failed. See: docker compose logs --tail=100 app (in the checkout). Fix the cause and run the installer again; it is safe to repeat.' 'docker compose up ba\u015far\u0131s\u0131z. Bak\u0131n: docker compose logs --tail=100 app (kurulum klas\u00f6r\u00fcnde). Nedeni giderip kurulumu yeniden \u00e7al\u0131\u015ft\u0131r\u0131n; tekrarlamak g\u00fcvenlidir.')
    }
    $url = "http://127.0.0.1:$(Get-AppPort)/health/live"
    Say ((M 'Waiting for {0} ...' '{0} bekleniyor ...') -f $url)
    for ($i = 0; $i -lt 120; $i++) {
        try {
            $response = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 5
            if ($response.StatusCode -eq 200) { Say (M 'HealtOS is up.' 'HealtOS \u00e7al\u0131\u015f\u0131yor.') Green; return $true }
        } catch {
            $null = $_
        }
        Start-Sleep -Seconds 5
    }
    Stop-Installer 1 (M 'HealtOS did not answer /health/live within 10 minutes. See: docker compose ps; docker compose logs --tail=100 app' 'HealtOS 10 dakika i\u00e7inde /health/live''a yan\u0131t vermedi. Bak\u0131n: docker compose ps; docker compose logs --tail=100 app')
}

# ---------------------------------------------------------------------------------------------
# Summary

function Show-Summary {
    param([bool]$Started, [string[]]$Missing)
    $port = Get-AppPort
    $url = "http://localhost:$port"
    Say-Step (M 'Summary' '\u00d6zet')
    if ($DryRun) {
        Say ((M '  Dry run finished: nothing was installed or started. Preview: {0}' '  Deneme bitti: hi\u00e7bir \u015fey kurulmad\u0131 ya da ba\u015flat\u0131lmad\u0131. \u00d6nizleme: {0}') -f $script:EnvResult)
    } elseif ($Started) {
        Say (M '  HealtOS is running.' '  HealtOS \u00e7al\u0131\u015f\u0131yor.') Green
    } elseif ($Missing.Count -gt 0) {
        Say (M '  Configured, NOT started: the values below are still missing.' '  Yap\u0131land\u0131r\u0131ld\u0131, BA\u015eLATILMADI: a\u015fa\u011f\u0131daki de\u011ferler h\u00e2l\u00e2 eksik.') Yellow
    } else {
        Say (M '  Configured (not started: -NoStart).' '  Yap\u0131land\u0131r\u0131ld\u0131 (ba\u015flat\u0131lmad\u0131: -NoStart).')
    }
    if ($Missing.Count -gt 0) {
        Say ((M '  Still missing in .env ({0} mode):' '  .env i\u00e7inde h\u00e2l\u00e2 eksik ({0} kipi):') -f $script:InstallMode)
        foreach ($item in $Missing) { Say ('    - ' + $item) }
        Say (M '    Google OAuth client: README step 3 / docs/INSTALL.md; webhook: docs/CLOUDFLARE_TUNNEL.md.' '    Google OAuth istemcisi: README 3. ad\u0131m / docs/INSTALL.md; webhook: docs/CLOUDFLARE_TUNNEL.md.')
        Say ((M '    Then: cd "{0}"; docker compose up -d --build   (or re-run this installer)' '    Sonra: cd "{0}"; docker compose up -d --build   (ya da bu kurulumu yeniden \u00e7al\u0131\u015ft\u0131r\u0131n)') -f $script:RepoDir)
    }
    Say ''
    Say ('  URL: ' + $url)
    if ($DryRun) {
        Say (M '  UI token: APP_UI_TOKEN in .env (printed once after the real run).' '  Aray\u00fcz anahtar\u0131: .env i\u00e7indeki APP_UI_TOKEN (ger\u00e7ek kurulumdan sonra bir kez yaz\u0131l\u0131r).')
    } else {
        Say ((M '  UI token: {0}' '  Aray\u00fcz anahtar\u0131: {0}') -f (Get-EnvValue 'APP_UI_TOKEN')) Green
        Say (M '  Keep the token private (password manager); it is the dashboard password.' '  Anahtar\u0131 gizli tutun (parola y\u00f6neticisi); panelin parolas\u0131d\u0131r.')
    }
    if ($script:EnvBackup) { Say ((M '  Previous .env: {0}' '  \u00d6nceki .env: {0}') -f $script:EnvBackup) }
    Say ''
    Say (M '  Next steps:' '  Sonraki ad\u0131mlar:')
    Say ((M '  - Open the URL and sign in with the UI token. Manage: cd "{0}"; docker compose ps; docker compose logs -f app' '  - Adresi a\u00e7\u0131p aray\u00fcz anahtar\u0131yla giri\u015f yap\u0131n. Y\u00f6netim: cd "{0}"; docker compose ps; docker compose logs -f app') -f $script:RepoDir)
    Say (M '  - Update later: git pull; docker compose up -d --build. Add a source: re-run with -Sources ...' '  - G\u00fcncelleme: git pull; docker compose up -d --build. Kaynak eklemek: -Sources ... ile yeniden \u00e7al\u0131\u015ft\u0131r\u0131n')
    if ($script:InstallSources -contains 'google') {
        Say ((M '  - Google: redirect URI in your OAuth client: {0}' '  - Google: OAuth istemcinizdeki y\u00f6nlendirme URI''si: {0}') -f (Get-RedirectUri))
        Say ((M '    Connect: {0}/auth/google/start, then: docker compose exec app health-mirror backfill' '    Ba\u011flan: {0}/auth/google/start, sonra: docker compose exec app health-mirror backfill') -f $url)
        Say (M '    GOOGLE_TOKEN_ENCRYPTION_KEY in .env: store it in a password manager too (a restore needs it).' '    .env i\u00e7indeki GOOGLE_TOKEN_ENCRYPTION_KEY: parola y\u00f6neticisinde de saklay\u0131n (geri y\u00fckleme ister).')
    }
    if ($script:InstallSources -contains 'garmin') {
        Say (M '  - Garmin: status: docker compose exec app health-mirror garmin-status; history: health-mirror garmin-backfill' '  - Garmin: durum: docker compose exec app health-mirror garmin-status; ge\u00e7mi\u015f: health-mirror garmin-backfill')
    }
    if ($script:InstallSources -contains 'apple') {
        Say (M '  - Apple Health: iPhone Health app > profile > Export All Health Data; upload export.zip on the System page' '  - Apple Sa\u011fl\u0131k: iPhone Sa\u011fl\u0131k > profil > T\u00fcm Sa\u011fl\u0131k Verilerini D\u0131\u015fa Aktar; export.zip''i Sistem sayfas\u0131ndan y\u00fckleyin')
        Say (M ([regex]::Unescape('    (Sistem > Apple Sa\u011fl\u0131k i\u00e7e aktar). Status: docker compose exec app health-mirror apple-status')) '    (Sistem > Apple Sa\u011fl\u0131k i\u00e7e aktar). Durum: docker compose exec app health-mirror apple-status')
    }
    if ($script:AppleReceiverChoice -eq 'yes') {
        $endpoint = Get-EnvValue 'APPLE_RECEIVER_ENDPOINT_URI'
        if ($endpoint) {
            Say ((M '    Receiver: POST {0}' '    Al\u0131c\u0131: POST {0}') -f $endpoint)
        } else {
            Say (M '    Receiver: set APPLE_RECEIVER_ENDPOINT_URI=https://<your host>/webhooks/apple-health in .env' '    Al\u0131c\u0131: .env i\u00e7inde APPLE_RECEIVER_ENDPOINT_URI=https://<adresiniz>/webhooks/apple-health ayarlay\u0131n')
        }
        if ($script:AppleTokenNew) {
            Say (M '    Add this header to the Health Auto Export REST API automation (shown once):' '    Bu ba\u015fl\u0131\u011f\u0131 Health Auto Export REST API otomasyonuna ekleyin (bir kez g\u00f6sterilir):')
            Say ('      Authorization: Bearer ' + $script:AppleTokenNew) Green
        } else {
            Say (M '    Header: Authorization: Bearer <content of APPLE_RECEIVER_TOKEN_HOST_FILE>' '    Ba\u015fl\u0131k: Authorization: Bearer <APPLE_RECEIVER_TOKEN_HOST_FILE i\u00e7eri\u011fi>')
        }
    }
    if ($script:BackupsChoice -eq 'yes') {
        Say ((M '  - Backups: daily at 03:30 (Task Scheduler: HealtOS Backup). Run one now: powershell -ExecutionPolicy Bypass -File .\scripts\backup.ps1  Log: {0}' '  - Yedekler: her g\u00fcn 03:30''da (G\u00f6rev Zamanlay\u0131c\u0131s\u0131: HealtOS Backup). \u015eimdi bir tane: powershell -ExecutionPolicy Bypass -File .\\scripts\\backup.ps1  G\u00fcnl\u00fck: {0}') -f (Join-Path (Get-HealtOSLogDir) 'backup.log'))
        if ($script:BackupKey) {
            Say ''
            Say ((M '  !!! COPY THE BACKUP KEY OFF THIS MACHINE NOW: {0}' '  !!! YEDEK ANAHTARINI \u015e\u0130MD\u0130 BU MAK\u0130NEN\u0130N DI\u015eINA KOPYALAYIN: {0}') -f $script:BackupKey) Red
            Say (M '  !!! Without it no backup can ever be read. Store it in a password manager (with .env''s GOOGLE_TOKEN_ENCRYPTION_KEY).' '  !!! Onsuz hi\u00e7bir yedek okunamaz. Parola y\u00f6neticisinde saklay\u0131n (.env''deki GOOGLE_TOKEN_ENCRYPTION_KEY ile birlikte).') Red
            if ($script:BackupPubKey) { Say ((M '  Public key: {0}' '  A\u00e7\u0131k anahtar: {0}') -f $script:BackupPubKey) }
            Say (M '  Backups are kept forever in BACKUP_HOST_DIR (default .\backups); copy them off the machine as well.' '  Yedekler BACKUP_HOST_DIR i\u00e7inde (varsay\u0131lan .\\backups) sonsuza dek saklan\u0131r; onlar\u0131 da makine d\u0131\u015f\u0131na kopyalay\u0131n.')
        }
    }
    if ($script:AutostartChoice -eq 'yes') {
        Say (M '  - Autostart: Docker Desktop starts when you sign in and brings HealtOS back; nothing runs until someone signs in.' '  - Otomatik ba\u015flatma: Docker Desktop oturum a\u00e7\u0131l\u0131nca ba\u015flar ve HealtOS''u geri getirir; biri oturum a\u00e7madan hi\u00e7bir \u015fey \u00e7al\u0131\u015fmaz.')
    }
    if ($script:UmayChoice -eq 'yes') {
        Say ((M '  - Umay: the bridge starts at sign-in (Task Scheduler: HealtOS Umay bridge). Log: {0}' '  - Umay: k\u00f6pr\u00fc oturum a\u00e7\u0131l\u0131nca ba\u015flar (G\u00f6rev Zamanlay\u0131c\u0131s\u0131: HealtOS Umay bridge). G\u00fcnl\u00fck: {0}') -f (Join-Path (Get-HealtOSLogDir) 'umay-bridge.log'))
    }
    if ($script:Notes.Count -gt 0) {
        Say ''
        Say (M '  Notes:' '  Notlar:')
        foreach ($note in $script:Notes) { Say ('  - ' + $note) }
    }
    Say ''
    Say (M '  Guide: docs/INSTALL.md (English), docs/INSTALL.tr.md (Turkish). Re-running this installer is safe.' '  Rehber: docs/INSTALL.tr.md (T\u00fcrk\u00e7e), docs/INSTALL.md (English). Kurulumu yeniden \u00e7al\u0131\u015ft\u0131rmak g\u00fcvenlidir.')
}

# ---------------------------------------------------------------------------------------------
# Main

function Invoke-HealtOSInstaller {
    $script:InstallLang = 'en'
    if ($Lang -and @('en', 'tr') -contains $Lang.ToLowerInvariant()) { $script:InstallLang = $Lang.ToLowerInvariant() }
    $script:Unattended = [bool]$NonInteractive
    $script:Notes = New-Object System.Collections.Generic.List[string]
    $noTerminal = $false
    if (-not $script:Unattended) {
        try { if ([Console]::IsInputRedirected) { $script:Unattended = $true; $noTerminal = $true } } catch { $null = $_ }
    }
    Test-Arguments
    if ($Help) { Show-Usage; return 0 }
    Select-Language
    Write-Host ''
    Write-Host "HealtOS installer $HEALTOS_INSTALLER_VERSION" -ForegroundColor Cyan
    $arch = $env:PROCESSOR_ARCHITECTURE
    if (-not $arch) { $arch = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture }
    Say ((M 'Detected: {0} ({1})' 'Alg\u0131lanan: {0} ({1})') -f (Get-HostLabel), $arch)
    if ($noTerminal) { Say (M 'No terminal for questions: continuing with options and defaults.' 'Soru i\u00e7in terminal yok: se\u00e7enekler ve varsay\u0131lanlarla devam ediliyor.') }
    if ($DryRun) { Say (M 'Dry run: every action is printed, nothing is changed.' 'Deneme \u00e7al\u0131\u015fmas\u0131: her ad\u0131m yaz\u0131l\u0131r, hi\u00e7bir \u015fey de\u011fi\u015ftirilmez.') }
    if (-not (Test-IsWindowsHost) -and -not $DryRun) {
        Stop-Installer 2 ((M 'install.ps1 installs HealtOS on Windows. On {0} use install.sh: curl -fsSL {1}/main/install.sh | bash' 'install.ps1 HealtOS''u Windows''a kurar. {0} \u00fczerinde install.sh kullan\u0131n: curl -fsSL {1}/main/install.sh | bash') -f (Get-HostLabel), $HealtOSRawUrl)
    }
    Select-Platform
    $script:RepoDir = Resolve-Checkout
    Say ((M 'Checkout: {0}' 'Kurulum klas\u00f6r\u00fc: {0}') -f $script:RepoDir)
    . (Join-Path (Join-Path (Join-Path $script:RepoDir 'deploy') 'windows') 'common.ps1')
    $script:EnvLines = $null
    Read-EnvLines
    Invoke-Preflight
    Select-Mode
    Select-Sources
    Select-SourceDetails
    Select-HostJobs
    Confirm-Plan
    Test-HostJobTools
    Update-EnvSettings
    Write-EnvFile
    Save-GarminKey
    Initialize-AppleToken
    Install-HostJobs
    $missing = Get-MissingProduction
    $started = $false
    if ($missing.Count -eq 0 -and -not $NoStart) { $started = Start-Stack }
    Show-Summary -Started $started -Missing $missing
    return 0
}

function Confirm-Plan {
    if ($script:Unattended) { return }
    $sources = @($script:InstallSources) -join ','
    if (-not $sources) { $sources = 'none' }
    Say ''
    Say ((M 'Plan: platform windows, mode {0}, sources {1}, backups {2}, autostart {3}, Umay {4}; checkout {5}.' 'Plan: platform windows, kip {0}, kaynaklar {1}, yedek {2}, otomatik ba\u015flatma {3}, Umay {4}; klas\u00f6r {5}.') -f $script:InstallMode, $sources, (ConvertTo-YesNoLabel $script:BackupsChoice), (ConvertTo-YesNoLabel $script:AutostartChoice), (ConvertTo-YesNoLabel $script:UmayChoice), $script:RepoDir)
    if ((Read-YesNo (M 'Go ahead?' 'Devam edilsin mi?') 'yes') -ne 'yes') {
        Stop-Installer 0 (M 'Nothing was changed.' 'Hi\u00e7bir \u015fey de\u011fi\u015ftirilmedi.')
    }
}

$script:BoundParameters = $PSBoundParameters
$HealtOSExitCode = 0
try {
    # Last value: stray output of a native command must never become the exit code.
    $HealtOSExitCode = [int](@(Invoke-HealtOSInstaller)[-1])
} catch {
    if ($_.FullyQualifiedErrorId -like 'HealtOSInstallerExit*') {
        $HealtOSExitCode = [int]$_.TargetObject
        if ($_.Exception.Message) {
            if ($HealtOSExitCode -eq 0) { Write-Host $_.Exception.Message } else { [Console]::Error.WriteLine($_.Exception.Message) }
        }
    } else {
        $HealtOSExitCode = 1
        [Console]::Error.WriteLine(((M 'Installer failed: {0}' 'Kurulum ba\u015far\u0131s\u0131z: {0}') -f $_.Exception.Message))
        [Console]::Error.WriteLine((M 'Nothing was deleted. Fix the cause and run the installer again; it is safe to repeat.' 'Hi\u00e7bir \u015fey silinmedi. Nedeni giderip kurulumu yeniden \u00e7al\u0131\u015ft\u0131r\u0131n; tekrarlamak g\u00fcvenlidir.'))
    }
}
if ($HealtOSInstallerFile) { exit $HealtOSExitCode }
$global:LASTEXITCODE = $HealtOSExitCode
