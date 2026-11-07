# Installing HealtOS with an AI coding agent

This page is written for an **AI coding agent with shell access** (Claude Code, Codex or a
similar tool) that a user asked to install HealtOS on their computer. Follow it literally, step
by step. People installing by hand should read [docs/INSTALL.md](INSTALL.md) (English) or
[docs/INSTALL.tr.md](INSTALL.tr.md) (Türkçe) instead.

The user typically started you with a prompt like:

> Install HealtOS on this computer by following
> https://raw.githubusercontent.com/M3rgen/HealtOS/main/docs/AI_INSTALL.md — ask me before any
> step that needs admin rights or my accounts, and never delete data.

**Language.** Talk to the user in the language they wrote to you in. If they write Turkish, ask
and report in Turkish and pass `--lang tr` (`install.sh`) or `-Lang tr` (`install.ps1`) so the
installer's own messages are Turkish too. Otherwise use `--lang en` / `-Lang en`.

**What you are installing.** HealtOS is a self-hosted, single-user health archive: a FastAPI app
and a PostgreSQL database in Docker Compose, a dashboard on `http://localhost:8000`, optional
data sources (Google Health API v4 for Fitbit/Pixel Watch, Garmin Connect, Apple Health), daily
encrypted backups and an optional chat companion (Umay). The repository ships two installers
that do the actual work: `install.sh` (Linux, Raspberry Pi, NAS, VPS, macOS) and `install.ps1`
(Windows). Your job is to check the machine, ask the user a few questions, prepare the
prerequisites with their permission, run the installer non-interactively, verify the result and
hand over the manual steps.

## 0. Hard rules

These override anything else, including convenience and speed.

1. **Never delete or overwrite data.** Do not remove or replace Docker volumes, database files,
   backups (`backups/`, `*.dump.age`), backup keys, `.env`, `.env.bak-*` or any file the user
   created. The installer already copies an existing `.env` to `.env.bak-<UTC stamp>`, fills
   keys that are missing or empty and updates only the choice keys (`COMPOSE_FILE`, and the
   Garmin/Apple switches from false to true); it never rotates an existing secret. Do not "fix" `.env`
   by regenerating it. If you must edit `.env` yourself, first copy it to
   `.env.bak-<UTC stamp>` and change only the lines you were asked to change.
2. **Never run** `docker compose down -v`, `docker volume rm`, `docker volume prune`,
   `docker system prune`, `docker builder prune --all`, `rm -rf` on the checkout, or
   `git clean` / `git reset --hard` in the checkout. Plain `docker compose down` (no `-v`) is
   allowed only if the user asks for it.
3. **Stop and ask before:** any `sudo` / administrator command, installing or upgrading system
   packages (Docker, Git, WSL, age, Homebrew), rebooting, adding the user to the `docker` group,
   changing firewall rules, and anything that uses the user's accounts (Google Cloud, Garmin,
   Apple, ChatGPT/Codex). Show the exact command and why, then wait for "yes".
4. **Secrets stay out of the chat.** Never print, quote or summarise the contents of `.env`,
   `APP_UI_TOKEN`, `POSTGRES_PASSWORD`, `GOOGLE_*` secrets, the Garmin key file, the Apple
   receiver token or the backup key; never write them into issues, commit messages, logs you
   share, or other files. Never `cat` the whole `.env`; when you need to check a key, test only
   whether it is set:

   ```sh
   grep -q '^APP_UI_TOKEN=.\+' .env && echo "APP_UI_TOKEN is set"
   ```

   The installer prints the UI token once at the end; it then sits in your tool output. Do not
   repeat it in your messages.
5. **Do not ask for passwords or OAuth secrets in chat** when the user can type them in
   themselves. For the Google client secret and similar values, offer two ways and recommend
   the first: (a) the user opens `.env` in an editor and fills the value; (b) the user runs the
   installer interactively in their own terminal (`./install.sh` / `.\install.ps1` without
   `--non-interactive`), which asks for the secret itself. Only if the user insists on giving it
   to you, use it once and do not echo it back. Never ask for the user's Google, Garmin, Apple or
   ChatGPT account password: those logins happen in the user's browser or app.
6. **`sudo` with a password.** Your shell usually has no terminal to type a password into. If a
   command needs `sudo` and `sudo -n true` fails, do not loop: give the user the exact command to
   run in their own terminal (in Claude Code they can also type it after a `!` prefix), and
   continue when they confirm. The installer itself does not need root: run as a normal user on
   Linux it installs its timers and services as user units (`systemctl --user`, lingering on);
   run with `sudo` it installs system units in `/etc/systemd/system`. Ask the user which they
   want; rootless is fine for a personal machine.
7. **Install on this machine only.** If the user wants HealtOS on a different machine (a server,
   a Raspberry Pi, a NAS), stop and ask: either they start you on that machine (for example over
   SSH), or they explicitly allow you to run commands there with `ssh`.
8. **Do not commit, push or open pull requests** in the HealtOS checkout. Do not upload anything.

## 1. Detect the machine

Run the checks for the host OS and keep the answers; you need them for step 2 and 3. Do not
install anything yet.

### Linux, Raspberry Pi, VPS, macOS (bash/zsh)

```sh
uname -s                      # Linux or Darwin
uname -m                      # x86_64 / amd64, aarch64 / arm64 (ok); armv7l / armv6l (32-bit: unsupported)
getconf LONG_BIT              # must be 64
cat /etc/os-release 2>/dev/null | head -n 4          # Linux distribution
sw_vers 2>/dev/null                                  # macOS version
cat /proc/device-tree/model 2>/dev/null; echo        # "Raspberry Pi …" on a Pi
command -v dpkg >/dev/null && dpkg --print-architecture   # arm64/amd64 ok; armhf = 32-bit userland
free -h 2>/dev/null || sysctl -n hw.memsize          # RAM (macOS prints bytes)
df -h "$HOME"                                        # free disk
nproc 2>/dev/null || sysctl -n hw.ncpu
id -u                                                # 0 = running as root
git --version
docker --version
docker compose version        # must be Compose v2 ("Docker Compose version v2.x")
docker info --format '{{.ServerVersion}}'            # fails if the daemon is not running or no permission
git rev-parse --show-toplevel 2>/dev/null            # inside a git checkout?
```

### Windows (PowerShell)

```powershell
[Environment]::OSVersion.Version; (Get-CimInstance Win32_OperatingSystem).Caption
$env:PROCESSOR_ARCHITECTURE                     # AMD64 or ARM64
[math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory/1GB,1)   # RAM in GB
Get-PSDrive C | Select-Object Used,Free
$PSVersionTable.PSVersion
wsl --status                                    # WSL 2 present and default version 2?
git --version
docker --version
docker compose version
docker info --format '{{.ServerVersion}}'
git rev-parse --show-toplevel 2>$null
```

### Interpret the results

- **Inside a HealtOS checkout?** Yes if the git top level contains `docker-compose.yml`,
  `install.sh` and `install.ps1`. Then use that directory and do not clone again.
- **Existing installation?** If the checkout has a `.env`, or `docker volume ls` lists a volume
  ending in `_postgres_data`, HealtOS was installed before. Tell the user. Re-running the
  installer is safe and keeps the data, the existing secrets and the backups; do not start over.
  A re-run keeps the previous mode by default: without `--mode` it reads the existing `.env`
  (`COMPOSE_FILE` without `docker-compose.dev.yml` means `full`, with it `trial`, none means
  `trial`); an explicit `--mode` / `-Mode` always wins, so pass it when the user wants to switch.
  A re-run applies the new choices: it rewrites `COMPOSE_FILE` and `COMPOSE_PATH_SEPARATOR`
  for the chosen mode and sources, turns `GARMIN_ENABLED` / `APPLE_RECEIVER_ENABLED` from
  `false` (or empty, `0`, `no`, `off`) to `true` when those are chosen, keeps `COMPOSE_FILE` entries
  it does not manage (and an existing `docker-compose.override.yml`), and replaces the Garmin key file path only when
  `--garmin-key-file` / `-GarminKeyFile` is given. It never changes a secret or `APP_ENV`
  (the Compose files set that), and never turns a source off. So on a re-run always pass the complete `--sources` list and the same mode,
  not only the source being added.
- **Architecture.** 64-bit x86 and 64-bit ARM are supported. `armv7l`, `armv6l`, `armhf` or
  `LONG_BIT` 32 means a 32-bit OS: stop and explain (see troubleshooting).
- **Resources (guideline).** At least 2 GB RAM (4 GB on a Raspberry Pi for the first build) and
  10 GB of free disk, plus room for the archive and backups. Below that, warn the user and ask
  whether to continue.
- **Docker.** Need Docker Engine or Docker Desktop with Compose v2 and a running daemon. `docker`
  present but `docker info` failing means "not running" or "no permission" (troubleshooting).

## 2. Ask the user (one message)

Ask everything in **one** message, show the detected facts and the defaults, and say that
answering "defaults" is fine. Do not ask about anything else up front.

> I checked this computer: <OS, architecture, RAM, free disk, Docker yes/no, Git yes/no,
> existing HealtOS yes/no>. A few choices, defaults in brackets; reply "defaults" to take them:
>
> 1. **Where should HealtOS run?** [detected platform]
>    1. Linux server / VM (Debian, Ubuntu, Fedora…; Proxmox VM/LXC): recommended
>    2. Raspberry Pi / ARM64 board: needs a 64-bit OS, 4 GB RAM or more; the first build is slow
>    3. NAS (Synology Container Manager, QNAP Container Station, Unraid, TrueNAS SCALE): set up
>       through the NAS web interface, I will guide you
>    4. macOS (Apple Silicon or Intel) with Docker Desktop (or OrbStack/Colima)
>    5. Windows 10/11 with Docker Desktop (WSL 2 backend)
>    6. Cloud VPS: like Linux; you open the dashboard through an SSH tunnel
> 2. **Mode** [trial]: *trial* = local look without Google, starts right away; *full* =
>    production mode, needs Google Health API credentials and an HTTPS webhook address (the
>    installer lists what is still missing).
> 3. **Data sources** [none]: Google Health (Fitbit, Pixel Watch), Garmin Connect, Apple Watch /
>    Apple Health, or none for now (you can add them later by re-running the installer).
> 4. **Daily encrypted backup** [yes in full mode, no in trial]
> 5. **Start automatically after a reboot** [yes] (Windows: Docker Desktop and HealtOS start
>    when you sign in)
> 6. **Umay chat bridge** [no]: needs the Codex CLI signed in to ChatGPT on this computer; your
>    questions and the archive excerpts Umay reads are sent to OpenAI.

Rules for the platform default: on a Linux host preselect `linux` (or `raspberrypi` when the
device model says Raspberry Pi); on macOS preselect `macos`; on Windows preselect `windows`. A
Linux setup cannot be installed on macOS or Windows and vice versa: if the user picks a platform
that does not match this machine, explain that and go back to rule 7 of step 0. NAS goes to
step 3d.

Follow-up questions only when needed:

- **Garmin chosen:** "Do you already have a Garmin Connect Personal API key saved in a file? If
  yes, tell me only the file path (not the key)." Default path:
  `~/.config/garmin/connect-personal-api-key` (Windows:
  `%USERPROFILE%\.config\garmin\connect-personal-api-key`). Check only that the file exists
  (`test -f PATH` / `Test-Path PATH`); never read it. Best: the user saves the key file first, then
  you install with `garmin`. If there is no file yet, the installers still finish but leave
  Garmin off and print a next-step note; it is turned on by re-running with `garmin` once the
  file exists (step 6). But an **explicit** `--garmin-key-file` / `-GarminKeyFile` that points to
  a missing file fails with exit code 2, so only pass the flag once the file exists. On Linux as a
  normal user with a shared primary group (for example `users`) the installer leaves Garmin off and
  prints `sudo chgrp 10001 <file> && sudo chmod 640 <file>`; ask the user to run it, then re-run.
- **Apple chosen:** "Do you want the Health Auto Export receiver (automatic sync from the iPhone
  app, needs your own HTTPS address), or only the export.zip upload?" Default: upload only
  (`--apple-receiver no`).
- **Google chosen:** do **not** ask for the client id or secret in chat (rule 5). The installer
  generates the token encryption key and prints the redirect URI; the user fills the OAuth values
  in step 6.

## 3. Prerequisites

Install only what step 1 found missing. Before every command block below: show it, say it needs
admin rights or downloads software, and wait for the user's yes (step 0, rule 3).

The installers need: `git`, Docker with Compose v2, and on Unix `curl` or `openssl` for secret
generation. `age` is needed only for backups (the installer prints a hint if it is missing).

### 3a. Linux server, VPS, Raspberry Pi (64-bit)

Debian, Ubuntu, Raspberry Pi OS (64-bit):

```sh
sudo apt-get update
sudo apt-get install -y git curl openssl ca-certificates age
# Docker Engine + Compose v2 from Docker's repository (official convenience script; show it to the user first):
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
sudo systemctl enable --now docker
```

Fedora:

```sh
sudo dnf install -y git curl openssl age dnf-plugins-core
sudo dnf config-manager addrepo --from-repofile=https://download.docker.com/linux/fedora/docker-ce.repo
sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
```

Arch / Manjaro:

```sh
sudo pacman -S --needed git curl openssl age docker docker-compose docker-buildx
sudo systemctl enable --now docker
```

Alpine: `sudo apk add git curl openssl age docker docker-cli-compose` and
`sudo rc-update add docker default && sudo service docker start`.

Running Docker without `sudo` (ask first; it gives the user root-equivalent rights through
Docker):

```sh
sudo usermod -aG docker "$USER"
```

The new group applies only to new login sessions. Either the user logs out and in and restarts
you, or you run the installer through `sg docker -c '…'` for this session. Running the whole
installer as root is also fine (default directory `/opt/healthos` then).

Raspberry Pi: the OS must be 64-bit (`uname -m` = `aarch64` and `dpkg --print-architecture` =
`arm64`). A 32-bit Raspberry Pi OS must be reflashed with the 64-bit image; that wipes the SD card,
so it is the user's decision and never something you do. Prefer an SSD over an SD card for the
database.

Proxmox LXC: Docker inside an LXC needs `nesting=1` (and usually `keyctl=1`) on the container;
the user changes that in Proxmox, not you. A VM avoids the question.

### 3b. macOS

```sh
xcode-select --install            # Git, if `git --version` asked to install developer tools
# Homebrew (https://brew.sh) is the simplest route; ask before installing it.
brew install git age
brew install --cask docker        # Docker Desktop; alternatives: `brew install --cask orbstack` or `brew install colima docker docker-compose`
open -a Docker                    # first start: the user accepts the terms in the Docker window
```

Wait until `docker info` succeeds (poll every 10 s, up to 3 minutes) before continuing. With
Colima: `colima start --cpu 2 --memory 4`, and make sure `docker compose version` works (the
Homebrew `docker-compose` formula prints how to register it as a CLI plugin).

### 3c. Windows 10/11

Run in PowerShell. `wsl --install` and Docker Desktop need administrator rights and usually a
reboot: ask first, and tell the user you will continue after the reboot when they start you
again.

```powershell
winget install -e --id Git.Git
wsl --install                      # if `wsl --status` showed no WSL 2; admin; reboot afterwards
winget install -e --id Docker.DockerDesktop
winget install -e --id FiloSottile.age
```

After the install the user starts Docker Desktop once, accepts the terms and keeps the default
"Use the WSL 2 based engine" setting. Open a new PowerShell window so the new `PATH` applies, and
wait until `docker info` succeeds. Windows on ARM64 works with Docker Desktop for ARM.

### 3d. NAS

Do not install packages on a NAS through its shell; NAS systems are managed through their web
interface and package center. Walk the user through the NAS section of
[docs/INSTALL.md](INSTALL.md) (Turkish: [docs/INSTALL.tr.md](INSTALL.tr.md)) one screen at a
time: the user installs Container Manager (Synology), Container Station (QNAP), the Compose
Manager plugin (Unraid) or uses Apps (TrueNAS SCALE) from the NAS interface and enables SSH.
Then one of two paths:

- **SSH and Git on the NAS** (you run there only with the user's explicit permission, step 0,
  rule 7): clone into a shared folder (for example `/volume1/docker/healtos` on Synology) and
  run step 4 with `--platform nas --no-start`. The installer writes `.env` and prints the NAS
  steps; it installs no services on a NAS. Start the stack over SSH from the checkout with
  `docker compose up -d --build` (often with `sudo` on Synology/QNAP; ask first), because the
  NAS web interfaces usually ignore the `COMPOSE_FILE` line in `.env`.
- **No Git on the NAS:** on the user's computer, in a fresh clone, run
  `./install.sh --non-interactive --platform nas --dry-run --env-out ./nas.env …`. The user
  copies the checkout to the NAS share and saves `nas.env` there as `.env`. It contains real
  secrets: do not open or print it. Paths in it (for example a Garmin key file) must be adjusted
  for the NAS. The user then creates the project in the NAS interface from that folder.

Backups on a NAS are a scheduled task the user creates in the NAS scheduler; the installer
prints the command and [docs/INSTALL.md](INSTALL.md) explains it.

## 4. Get the code and run the installer

### 4a. Clone (skip if step 1 found a HealtOS checkout)

Default location: `~/healtos` (Linux/macOS user), `/opt/healthos` (Linux as root),
`%USERPROFILE%\healtos` (Windows). If that directory already exists and is not empty, do not
touch it: ask the user for another `--dir`.

```sh
git clone https://github.com/M3rgen/HealtOS.git ~/healtos
cd ~/healtos
```

```powershell
git clone https://github.com/M3rgen/HealtOS.git "$env:USERPROFILE\healtos"
Set-Location "$env:USERPROFILE\healtos"
```

To pin a release, add `--branch v0.2.2` to the clone (the installer's equivalent is
`--ref v0.2.2` / `-Ref v0.2.2` when it clones by itself). Running the installer from inside the
checkout is preferred over the `curl … | bash` and `irm … | iex` one-liners because you can read
the script first.

### 4b. Map the answers to flags

| Answer | `install.sh` | `install.ps1` |
| --- | --- | --- |
| Language | `--lang en` / `--lang tr` | `-Lang en` / `-Lang tr` |
| Linux server / VM | `--platform linux` | not available |
| Raspberry Pi / ARM64 | `--platform raspberrypi` | not available |
| NAS (over SSH, see 3d) | `--platform nas` | not available |
| macOS | `--platform macos` | not available |
| Windows | not available | `-Platform windows` |
| Cloud VPS | `--platform vps` | not available |
| Trial / full | `--mode trial` / `--mode full` | `-Mode trial` / `-Mode full` |
| Sources | `--sources google,garmin,apple` (any subset) or `--sources none` | `-Sources google,garmin,apple` or `-Sources none` |
| Garmin key file path (must exist when given) | `--garmin-key-file PATH` | `-GarminKeyFile PATH` |
| Apple receiver | `--apple-receiver yes\|no` (default no) | `-AppleReceiver yes\|no` |
| Backups | `--backups yes\|no` | `-Backups yes\|no` |
| Autostart | `--autostart yes\|no` | `-Autostart yes\|no` |
| Umay bridge | `--umay yes\|no` | `-Umay yes\|no` |
| Other directory | `--dir PATH` | `-Dir PATH` |
| Never prompt | `--non-interactive` (or `--yes`) | `-NonInteractive` |
| Plan only | `--dry-run` | `-DryRun` |
| Configure, do not start | `--no-start` | `-NoStart` |

Always pass every flag explicitly (no hidden defaults) and always pass `--non-interactive` /
`-NonInteractive`: your shell cannot answer prompts. Do not pass `--google-client-id` /
`--google-client-secret` unless the user explicitly asked you to (step 0, rule 5). `./install.sh
--help` and `.\install.ps1 -Help` print the full, current usage; if it differs from this table,
the script wins.

### 4c. Dry run first and show the plan

Unix (example: Linux server, trial, no sources, defaults):

```sh
./install.sh --non-interactive --dry-run --lang en --platform linux --mode trial \
  --sources none --backups no --autostart yes --umay no
```

Windows:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -NonInteractive -DryRun `
  -Lang en -Platform windows -Mode trial -Sources none -Backups no -Autostart yes -Umay no
```

(`-ExecutionPolicy Bypass` applies to this one process only and needs no admin rights. With
PowerShell 7 use `pwsh` instead of `powershell`.)

A dry run changes nothing: no Docker, no services, no clone. It prints every action and writes
the `.env` it would write to `.env.dry-run` (or `--env-out PATH`). Summarise the planned actions
for the user in plain words: which compose files, which services or scheduled tasks (backup,
autostart, Umay) it would install, where the backup key would go, and which steps need admin
rights. Do not show the secret values from `.env.dry-run`. Ask: "Shall I run it for real?"

### 4d. Real run

Same command without `--dry-run` / `-DryRun`. The first run builds the images (a few minutes;
20 to 40 minutes on a Raspberry Pi), then waits for `/health/live` itself (up to 5 minutes, 15
on a Raspberry Pi, 10 on Windows). Run it in the foreground with a long timeout or in the
background and wait; do not interrupt it. If your tool has a time limit, run the installer with
`--no-start` / `-NoStart` first and then start the stack yourself in the background:

```sh
docker compose up -d --build
```

`.env` sets `COMPOSE_FILE`, so plain `docker compose …` in the checkout selects the right files
afterwards; do not add `-f` options, and make sure `COMPOSE_FILE` is not set in your own shell
environment.

Exit codes: `0` ok, `1` a step failed (read the last lines, see troubleshooting, fix, re-run the
same command; it is safe to repeat), `2` bad arguments or unsupported environment (fix the flags
or the prerequisite; do not work around it). A platform that does not match this machine is
exit 2 (a dry run only warns).

Read the installer's summary and keep these facts for step 8 (without the secret values):

- **"Configured, NOT started"** in full mode means values are still missing (Google client,
  webhook URL…); the installer lists them and exits 0. Report the list; do not invent values.
- **Notes** at the end: for example "Make Docker start at boot: sudo systemctl enable docker" or
  "sudo loginctl enable-linger <user>" on a rootless Linux install. These need the user's
  permission and usually their own terminal (step 0, rule 6).
- **`.env.bak-<stamp>`**: created when an existing `.env` was filled; it is kept.
- The summary prints the UI token and, with the Apple receiver, its `Authorization: Bearer`
  header once. Do not repeat either (step 0, rule 4).

## 5. Verify

From the checkout directory:

```sh
docker compose ps                                   # app and postgres "running" / "healthy"
for i in $(seq 1 60); do curl -fsS http://localhost:8000/health/live && break; sleep 10; done
```

```powershell
docker compose ps
foreach ($i in 1..60) { try { Invoke-RestMethod http://localhost:8000/health/live; break } catch { Start-Sleep 10 } }
```

Use the port from `APP_PORT` in `.env` if it is not 8000. On a Raspberry Pi allow up to 30
minutes for the first start. `/health/live` needs no token. If it never answers, run
`docker compose logs --tail=100 app` and see troubleshooting; you may show those logs to the user
(they hold no secrets), but do not post them anywhere.

Then tell the user:

- Open `http://localhost:8000` (on a VPS or remote server: first
  `ssh -L 8000:127.0.0.1:8000 user@server` on their own computer, then the same address).
- The login key is `APP_UI_TOKEN` in `<checkout>/.env`. Let them read it themselves, for example
  by opening `.env` in an editor or running `grep '^APP_UI_TOKEN=' .env` in their own terminal.
  Do not read it out for them.
- In trial mode the dashboard starts empty and reports that Google is not configured: that is
  expected.

## 6. Manual follow-ups per data source

These need the user's accounts and browser. Explain each one, point at the documentation and
offer to do the shell parts after they finish. Never ask for their account passwords.

**Google Health (Fitbit, Pixel Watch).** Note first: as of October 2026 Google is not onboarding
new Google Health API projects; real sync needs an eligible Google Cloud project.

1. The user follows Google's setup guide (https://developers.google.com/health/setup): links
   their account in the Google Health app, enables the API, adds themselves as a test user, adds
   the read-only `googlehealth.*` scopes and creates an OAuth **Web application** client.
2. Authorized redirect URI: `http://localhost:8000/auth/google/callback` (the installer prints
   it; it must match `GOOGLE_REDIRECT_URI` in `.env`, including the port).
3. The user puts `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` into `.env` themselves (the
   installer already generated `GOOGLE_TOKEN_ENCRYPTION_KEY`; never change it once tokens are
   stored).
4. Apply and connect: `docker compose up -d --force-recreate app`, then the user opens
   `http://localhost:8000/auth/google/start` and completes consent in the browser.
5. Check: `docker compose exec app health-mirror probe --days 30`, then
   `docker compose exec app health-mirror sync steps` and `docker compose exec app health-mirror status`.

Full mode additionally needs an HTTPS `GOOGLE_HEALTH_WEBHOOK_ENDPOINT_URI` and
`GOOGLE_HEALTH_WEBHOOK_AUTHORIZATION`; see [docs/DEPLOYMENT.md](DEPLOYMENT.md) and
[docs/CLOUDFLARE_TUNNEL.md](CLOUDFLARE_TUNNEL.md). Google's Testing-mode refresh tokens expire
after seven days.

**Garmin Connect.** The user creates a Personal API key in Garmin Connect and saves it in a file
that only they can read (`chmod 600` on Unix). You may create the empty directory
(`mkdir -p ~/.config/garmin`) but the user pastes the key into the file. Then re-run the
installer with `garmin` added to `--sources` and `--garmin-key-file PATH`, and check with
`docker compose exec app health-mirror garmin-status`. Never `cat` the key file. Garmin keys
expire; Garmin e-mails a week before; replacing the file content is enough. Details:
[Garmin Connect Personal API](REFERENCE.md#garmin-connect-personal-api).

**Apple Watch / Apple Health.**

- Upload (always available): on the iPhone, Health › profile picture › *Export All Health
  Data*; move `export.zip` to this computer; in HealtOS open the System page, "Apple Sağlık içe
  aktar" › choose the file › *Yükle*. Uploading a newer export later adds only new records.
- Health Auto Export receiver (only if chosen with `--apple-receiver yes`): the installer
  turns it on and creates the token file (`APPLE_RECEIVER_TOKEN_HOST_FILE` in `.env`; default
  `~/.config/healthos/apple-receiver-token`, Windows `%APPDATA%\HealtOS\apple-receiver-token`)
  and prints the header once. The user reads the token from that file themselves; do not read
  or repeat it. The receiver needs an HTTPS address of the user's own (tunnel or reverse proxy)
  that forwards only `POST /webhooks/apple-health`; the user sets
  `APPLE_RECEIVER_ENDPOINT_URI` to it in `.env`, applies it with `docker compose up -d`, and
  adds a REST API automation in the app with that URL, the header
  `Authorization: Bearer <token>`, JSON, export version 2. Details:
  [Apple Health](REFERENCE.md#apple-health-github-version).

**Backups (if enabled).** The installer creates an age backup key outside the repository and
outside `.env`: Linux `/etc/healthos/backup.key` (root) or `~/.config/healtos/backup.key`,
macOS `~/Library/Application Support/HealtOS/backup.key`, Windows `%APPDATA%\HealtOS\backup.key`.
Tell the user, clearly: **copy this key off this computer now (for example into a password
manager); without it every backup is unreadable.** Keep `GOOGLE_TOKEN_ENCRYPTION_KEY` with it.
If `age` was missing, the installer printed how to install it and no key was made; install
`age` (step 3, with permission) and re-run the same installer command. Offer to run one backup
now with the command the installer printed (Linux: `systemctl --user start
healthos-backup.service`, or `sudo systemctl start healthos-backup.service` for system units;
macOS: `launchctl kickstart gui/$(id -u)/org.healtos.backup`; Windows: `powershell -ExecutionPolicy Bypass -File .\scripts\backup.ps1`) and check
`.\backups` / `backups/` for a new `health-<stamp>.dump.age`. Backups are kept forever unless the
user sets `BACKUP_RETENTION_DAYS`.

**Umay (if enabled).** The bridge needs the Codex CLI installed and signed in to ChatGPT on this
host (`codex login`, done by the user). See [Local Codex chat](REFERENCE.md#local-codex-chat-umay-bridge).

## 7. Troubleshooting

| Symptom | Cause | What to do |
| --- | --- | --- |
| `Cannot connect to the Docker daemon` / `docker info` fails | Docker not running | Linux: ask, then `sudo systemctl start docker`. macOS: `open -a Docker` (or `colima start`). Windows: start Docker Desktop. Wait until `docker info` works. |
| `permission denied … /var/run/docker.sock` | User not in the `docker` group | Ask, then `sudo usermod -aG docker "$USER"`; new login (or `sg docker -c '…'`). Or run the installer with `sudo`. |
| `docker: 'compose' is not a docker command` | Compose v2 plugin missing | Install `docker-compose-plugin` (Docker repo), `docker-compose` (Arch/Homebrew, registered as plugin) or update Docker Desktop. Do not use the old `docker-compose` v1. |
| Port 8000 already in use (`bind: address already in use`) | Another program on 8000 | Find it: `ss -ltnp \| grep :8000` (Linux), `lsof -nP -iTCP:8000 -sTCP:LISTEN` (macOS), `Get-NetTCPConnection -LocalPort 8000 -State Listen` (Windows). Ask the user; then back up `.env`, set `APP_PORT=8080` and `GOOGLE_REDIRECT_URI=http://localhost:8080/auth/google/callback` in it, and re-run the installer (or `docker compose up -d`). Use the new port in step 5 and in the Google redirect URI. Never stop the other program without asking. |
| `WSL 2 installation is incomplete` / Docker Desktop will not start on Windows | WSL 2 missing or virtualization off | Ask, then `wsl --install` (admin) and reboot; `wsl --update`. If virtualization is disabled in the firmware, the user enables it in BIOS/UEFI. |
| `exec format error`, `no matching manifest for linux/arm/v7` | 32-bit ARM OS | Unsupported. The user installs a 64-bit OS (Raspberry Pi OS 64-bit); reflashing erases the card, so it is their decision. |
| First build on a Raspberry Pi takes very long | Building the frontend and Python wheels on ARM | Normal: 20 to 40 minutes. Do not interrupt; use an SSD and 4 GB RAM or more; check `docker compose ps` / `docker compose logs -f app` while waiting. |
| Build fails with `no space left on device` | Disk full | Show `df -h` and `docker system df`. Do not prune anything yourself; ask the user what may be freed. |
| Warning "Port 5432 is in use" in trial mode | A local PostgreSQL already runs | Trial mode publishes the database on `127.0.0.1:5432`. Ask the user: stop their other PostgreSQL themselves, or use `--mode full` once the Google values exist. Never stop it yourself. |
| Installer says "Configured, NOT started" / app refuses to start in production | Full mode without Google or webhook values | Expected until step 6 is done. The user fills the listed values in `.env` and you re-run the same command, or re-run with `--mode trial` to look around first. |
| `git: command not found`, installer exits with 2 | Git missing | Step 3 for the OS, then re-run. |
| Installer exit 2 for the platform | Platform does not match the host | Pick the host's platform (step 2). |
| Login page refuses the key | Wrong or truncated token | The user copies `APP_UI_TOKEN` from `.env` again (no quotes, no spaces). Never regenerate it on an existing install without the user asking. |

If something is not covered here, read [docs/INSTALL.md](INSTALL.md) and the
[technical reference](REFERENCE.md#troubleshooting), explain what you found and ask before
trying anything that is not clearly safe.

## 8. Final report to the user

End with a short checklist in the user's language, without any secret values:

- [ ] Installed in: `<checkout path>`; platform, mode, sources
- [ ] Dashboard: `http://localhost:<port>` (`/health/live` answered); remote: SSH tunnel command
- [ ] Login key: `APP_UI_TOKEN` in `<checkout>/.env` (read it yourself)
- [ ] `.env` backups the installer made: `.env.bak-…` (kept; nothing was deleted)
- [ ] Daily backup: on/off; key location; **copy the key off this computer**
- [ ] Autostart after reboot: on/off; Umay bridge: on/off
- [ ] Still to do by you: Google OAuth + Connect Google / Garmin key file / Apple export upload or
      receiver token / anything that failed or was skipped, with the exact next command
- [ ] Re-running the same installer command later is safe; it keeps data and secrets
