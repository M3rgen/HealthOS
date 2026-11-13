# Installing HealthOS

*Türkçe: [INSTALL.tr.md](INSTALL.tr.md).*

This guide covers the guided installer (`install.sh` for Linux, Raspberry Pi, NAS, VPS and macOS;
`install.ps1` for Windows), every supported platform, the data sources, backups, updates and
troubleshooting. The installer only automates the steps of the manual setup in the
[README](../README.md#manual-setup) and the [deployment guide](DEPLOYMENT.md); you can always do
them by hand instead.

**The default is a Linux server.** HealthOS is meant to run on a machine that stays on: a Linux
server or VM, a Raspberry Pi or a NAS. A desktop or laptop works too, but the archive only stays
current while it is awake.

- [Quick start](#quick-start) · [Install with an AI agent](#install-with-an-ai-agent)
- [What the installer asks](#what-the-installer-asks)
- [Command-line options](#command-line-options)
- [What the installer changes, and what it never does](#what-the-installer-changes-and-what-it-never-does)
- [Platforms](#platforms): [Linux](#linux-server-or-vm-default) ·
  [Raspberry Pi](#raspberry-pi-and-other-arm64-boards) · [NAS](#nas-synology-qnap-unraid-truenas-scale) ·
  [macOS](#macos) · [Windows](#windows-10-and-11) · [Cloud VPS](#cloud-vps)
- [Data sources](#data-sources)
- [Backups](#backups)
- [Updating](#updating)
- [Stopping and uninstalling without losing data](#stopping-and-uninstalling-without-losing-data)
- [Troubleshooting](#troubleshooting)

## Quick start

**Linux, Raspberry Pi, VPS, macOS** (bash; run as your normal user or as root). The installer never calls `sudo` itself:
as root it installs system units, as a normal user it installs user units, and any command that needs
`sudo` is printed for you to run:

```sh
curl -fsSL https://raw.githubusercontent.com/M3rgen/HealthOS/v0.2.2.1/install.sh | bash
```

**Windows 10/11** (PowerShell, as your normal user):

```powershell
irm https://raw.githubusercontent.com/M3rgen/HealthOS/v0.2.2.1/install.ps1 | iex
```

Both commands install a release (here v0.2.2.1): the installer clones its own tag
(`HEALTHOS_INSTALLER_VERSION`), never the moving `main` branch, unless you ask for it.

**The development version** (`main`, not a release):

```sh
curl -fsSL https://raw.githubusercontent.com/M3rgen/HealthOS/main/install.sh | bash -s -- --ref main
```

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/M3rgen/HealthOS/main/install.ps1))) -Ref main
```

**Prefer to read the script before running it?** Clone first, read `install.sh` or `install.ps1`,
then run it from the checkout; it then uses that checkout and clones nothing:

```sh
git clone --branch v0.2.2.1 https://github.com/M3rgen/HealthOS.git
cd HealthOS
less install.sh
./install.sh            # or: ./install.sh --dry-run   to see every action first
```

```powershell
git clone --branch v0.2.2.1 https://github.com/M3rgen/HealthOS.git
cd HealthOS
notepad .\install.ps1
powershell -ExecutionPolicy Bypass -File .\install.ps1     # or add -DryRun
```

### Install with an AI agent

A coding agent with shell access (Claude Code, OpenAI Codex CLI or similar) can run the
installation for you by following [AI_INSTALL.md](AI_INSTALL.md). Give it this prompt:

```text
Install HealthOS on this computer by following https://raw.githubusercontent.com/M3rgen/HealthOS/v0.2.2.1/docs/AI_INSTALL.md — ask me before any step that needs admin rights or my accounts, and never delete data.
```

Ready to run in a terminal:

```sh
claude "Install HealthOS on this computer by following https://raw.githubusercontent.com/M3rgen/HealthOS/v0.2.2.1/docs/AI_INSTALL.md — ask me before any step that needs admin rights or my accounts, and never delete data."
codex "Install HealthOS on this computer by following https://raw.githubusercontent.com/M3rgen/HealthOS/v0.2.2.1/docs/AI_INSTALL.md — ask me before any step that needs admin rights or my accounts, and never delete data."
```

Or paste the prompt into Claude Code, Codex or any other agent that can run commands. The agent
asks you the same questions as the installer, shows you a `--dry-run` first, and then runs the
installer non-interactively with your answers.

### What a run does

In order:

1. Checks the host: Git, Docker with Compose v2 and a running Docker engine. If something is
   missing it says how to install it on your OS and stops (exit code 2); it installs no system
   packages by itself.
2. If it is not running inside a HealthOS checkout, clones `https://github.com/M3rgen/HealthOS.git`
   at `--ref` (default: the installer's own release tag; `main` only with `--ref main`) into the
   checkout directory.
3. Asks the questions below (or takes them from flags).
4. Writes `.env`: new secrets only where a value is missing, an existing `.env` is copied to
   `~/.local/state/healthos/env-backups/` (Windows: `%LOCALAPPDATA%\HealthOS\env-backups\`) first; a re-run may change only the few values listed in
   [What the installer changes](#what-the-installer-changes-and-what-it-never-does).
   Secrets are never replaced.
5. Optionally creates the backup key and schedules the daily backup, autostart and the Umay bridge
   with the host's own scheduler (systemd, launchd or Task Scheduler).
6. Builds and starts the stack (`docker compose up -d --build`) unless you passed `--no-start` or
   full-mode values are still missing, waits for `/health/live`, then prints the dashboard URL, the access token (once) and the next
   steps.

Then open **<http://localhost:8000>** on the machine (through an SSH tunnel for a server, see
[below](#open-the-dashboard-from-another-computer)) and enter the token. The first build downloads
dependencies and takes a few minutes; on a Raspberry Pi much longer.

## What the installer asks

Every question has a preselected answer; Enter accepts it. The questions are in English or Turkish.

1. **Language.** English (preselected) or Türkçe.
2. **Platform.** All six are always listed. The default is Linux on a Linux host; on macOS or
   Windows the installer says what it detected and preselects that host's own platform, because a
   Linux setup cannot be installed there.

   | # | Platform | Note |
   | --- | --- | --- |
   | 1 | Linux server / VM (Debian, Ubuntu, Fedora…; Proxmox VM/LXC) | default, recommended |
   | 2 | Raspberry Pi / ARM64 board | 64-bit OS required; 4 GB RAM or more recommended; first build is slow |
   | 3 | NAS (Synology Container Manager, QNAP Container Station, Unraid, TrueNAS SCALE) | prepares `.env`; the rest follows [the NAS section](#nas-synology-qnap-unraid-truenas-scale) |
   | 4 | macOS (Apple Silicon or Intel) | Docker Desktop, OrbStack or Colima |
   | 5 | Windows 10/11 | Docker Desktop with the WSL 2 backend |
   | 6 | Cloud VPS | same as Linux, plus the SSH-tunnel note |

3. **Mode.** `full` or `trial`; see [Trial and full mode](#trial-and-full-mode). On a first run `full`
   is preselected; Google is not needed for it. On a re-run the preselected answer (and the default
   with `--yes`) comes from the existing `.env`: a `COMPOSE_FILE` without `docker-compose.dev.yml`
   means `full`, one with it means `trial`, and no `.env` or no `COMPOSE_FILE` means `full`. `--mode`
   always wins. `trial` is only for looking around and leaving; the installer says so every time:
   do not keep data in it, there are no backups.
4. **Data sources.** Any combination of Google Health API v4, Garmin Connect Personal API and Apple
   Health, or none (the default); each is described under [Data sources](#data-sources). The choice
   is written to `.env` as `HEALTHOS_SOURCES` (for example `garmin,apple`, or `none`); a source the
   existing `.env` already has on stays in it (remove it there by hand). For Google it asks for
   the OAuth client ID and secret (Enter leaves them empty for later) and, in full mode, the public
   webhook host name; for Garmin, the path of the key file (if the file does not exist yet, you can
   paste the key and it is saved there); for Apple, whether to enable the Health Auto Export
   receiver (default no) and its HTTPS address.
5. **Checkout directory.** Only when it has to clone: `/opt/healthos` when run as root on Linux,
   otherwise `~/healthos` (`%USERPROFILE%\healthos` on Windows). The directory must be empty or
   missing; the installer never clones over existing files.
6. **Daily encrypted backup.** Default yes in full mode, no in trial mode. If no backup key exists,
   it offers to create one; see [Backups](#backups).
7. **Start after reboot.** Default yes.
8. **Umay bridge as a service.** Default no; see [Umay](../README.md#optional-umay).

Finally it shows the plan (platform, mode, sources, directory, host jobs, `COMPOSE_FILE`) and asks
for confirmation; answering no changes nothing.

### Trial and full mode

| | `trial` | `full` |
| --- | --- | --- |
| Purpose | Only to look around and leave (development): do not keep data in it, no backups | Real use: keeps your data, unattended operation, daily encrypted backups by default |
| Compose files | `docker-compose.yml` + `docker-compose.dev.yml` | `docker-compose.yml` |
| `APP_ENV` (set by the Compose files, not in `.env`) | `development` (the production secret guard is skipped) | `production` |
| Google | optional | optional; only with `google` in `HEALTHOS_SOURCES`: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_TOKEN_ENCRYPTION_KEY`, `GOOGLE_HEALTH_WEBHOOK_ENDPOINT_URI` (HTTPS) and `GOOGLE_HEALTH_WEBHOOK_AUTHORIZATION` |
| `HEALTHOS_SOURCES` | the chosen sources | the chosen sources |
| PostgreSQL port | published on `127.0.0.1:5432` | not published |
| API docs at `/docs` | yes | no |

In both modes `docker-compose.garmin.yml` and `docker-compose.apple.yml` are added when Garmin or
the Apple receiver is chosen.

**Re-runs keep the mode.** Without `--mode`, a re-run reads the existing `.env`: a `COMPOSE_FILE`
that lacks `docker-compose.dev.yml` means `full`, one that has it means `trial`, and no `.env` or no
`COMPOSE_FILE` means `full`. The wizard preselects the same answer. Pass `--mode` to switch.

In full mode the app refuses to start until every production value of the chosen sources is set:
`APP_UI_TOKEN` always, the Google values only with `google` in `HEALTHOS_SOURCES`, a readable key
file with `garmin`. The installer lists
what is still missing and then configures without starting, instead of letting the container
fail later; fill the values into `.env` and run the installer again (or
`docker compose up -d --build`).

**Moving from trial to full later.** Run the installer again with `--mode full`. It rewrites
`COMPOSE_FILE` to match the new choice (the development override drops out; other files you
listed there yourself are kept), never replaces a secret, and lists the production values that
are still missing. Your data stays in the same database volume.

## Command-line options

`install.sh` and `install.ps1` take the same options. Without options the installer asks; with
`--yes` / `-NonInteractive` it never asks and uses the flags and the defaults.

| `install.sh` | `install.ps1` | Meaning |
| --- | --- | --- |
| `--lang en\|tr` | `-Lang en\|tr` | Language of the questions and messages. Interactive default: ask, English preselected. |
| `--platform linux\|raspberrypi\|nas\|macos\|windows\|vps` | `-Platform …` | Target platform (see the table above). |
| `--mode full\|trial` | `-Mode full\|trial` | Default `full` on a first run; on a re-run the mode of the existing `.env` (see [Trial and full mode](#trial-and-full-mode)). An explicit value always wins. `trial` is only for looking around: no backups, do not keep data in it. |
| `--sources google,garmin,apple` or `none` | `-Sources …` | Comma-separated data sources (default `none`). Written to `.env` as `HEALTHOS_SOURCES`, together with any source the existing `.env` still has on. |
| `--dir PATH` | `-Dir PATH` | Checkout directory. Default: the current checkout if run inside one; otherwise Linux `/opt/healthos` (root) or `~/healthos`, macOS `~/healthos`, Windows `%USERPROFILE%\healthos`. |
| `--ref REF` | `-Ref REF` | Git branch or tag to clone when bootstrapping. Default: the release tag of the installer itself (`v` + `HEALTHOS_INSTALLER_VERSION`, e.g. `v0.2.2`); `main` only with `--ref main`. |
| `--garmin-key-file PATH` | `-GarminKeyFile PATH` | Garmin Personal API key file on the host. An explicit path that does not exist is an error (exit code 2). Without the flag, a missing default key file leaves Garmin off with a note and exit code 0. |
| `--google-client-id ID` | `-GoogleClientId ID` | Google OAuth client ID. |
| `--google-client-secret S` | `-GoogleClientSecret S` | Google OAuth client secret. |
| `--apple-receiver yes\|no` | `-AppleReceiver yes\|no` | Enable the Health Auto Export receiver (default no). |
| `--backups yes\|no` | `-Backups yes\|no` | Schedule the daily encrypted backup (default yes in full mode, no in trial mode). |
| `--autostart yes\|no` | `-Autostart yes\|no` | Bring HealthOS back after a reboot (default yes). |
| `--umay yes\|no` | `-Umay yes\|no` | Install the Umay bridge as a service (default no). |
| `--yes`, `--non-interactive` | `-NonInteractive` | Never prompt; take the flags and the defaults. |
| `--dry-run` | `-DryRun` | Print every action without doing it: no clone, no Docker, no service. The `.env` it would write goes to `--env-out` (default `<dir>/.env.dry-run`); it never overwrites a real file. |
| `--env-out PATH` | `-EnvOut PATH` | Where `--dry-run` writes the `.env` it would write. Only valid together with `--dry-run` (otherwise exit code 2); refuses the checkout's `.env`, any `.env.bak-*` and any existing file that is not an earlier preview. |
| `--no-start` | `-NoStart` | Configure only; do not build or start the stack. |
| `--help` | `-Help` | Usage in English and Turkish. |

Exit codes: `0` success, `1` a step failed, `2` bad arguments or an unsupported environment
(for example Git or Docker missing, a 32-bit OS).

Command-line values such as `--google-client-secret` can end up in your shell history; leave them
out and let the installer ask, or edit `.env` afterwards.

Examples:

```sh
# Linux server, unattended, full mode with Google and Garmin:
./install.sh --yes --platform linux --mode full --sources google,garmin \
  --garmin-key-file ~/.config/garmin/connect-personal-api-key

# See exactly what would happen on a NAS, without touching anything:
./install.sh --platform nas --dry-run

# Configure only, start later yourself:
./install.sh --mode trial --sources none --no-start
```

```powershell
.\install.ps1 -NonInteractive -Platform windows -Mode trial -Sources apple
```

## What the installer changes, and what it never does

HealthOS keeps every record and every backup; the installer follows the same rule.

- **`.env` is copied before every change.** If it exists, it is first copied to
  `~/.local/state/healthos/env-backups/` (Windows: `%LOCALAPPDATA%\HealthOS\env-backups\`), a directory only you can open (700, files 600), as
  `<checkout path>.env.bak-<UTC stamp>`; the summary says how many copies of this checkout are
  there. Older `.env.bak-*` files next to `.env` (made by earlier versions) are copied there once
  and stay where they are; nothing is moved or deleted. Then the installer fills keys that are
  missing or empty and may change only these existing values: `COMPOSE_FILE` and
  `COMPOSE_PATH_SEPARATOR` (they follow your current mode and sources), `HEALTHOS_SOURCES` (the
  sources you choose, plus every source the existing `.env` already runs: full mode always adds
  `google`, and an older `.env` without the key counts as Google plus Garmin while its older
  `GARMIN_ENABLED` switch is on and a key file is set), `APPLE_RECEIVER_ENABLED` from `false` to
  `true` when you turn the receiver on, and the Garmin key
  file path when you pass `--garmin-key-file` / `-GarminKeyFile` explicitly. Each change is
  reported. A flag is switched to `true` only when its value is empty or one of `false`, `0`, `no`,
  `off` (any case). It never turns a source off (set `…_ENABLED=false` yourself), and every other existing
  value stays as it is. An existing secret is never replaced: a new `POSTGRES_PASSWORD` would lock
  you out of the existing database. `APP_ENV` is not written; the Compose files set it.
- **Secrets** are generated with `openssl` or `/dev/urandom` (`install.sh`) or .NET's random
  number generator (`install.ps1`); no Python is needed on the host. `POSTGRES_PASSWORD` is 64
  hexadecimal characters, `APP_UI_TOKEN` at least 43 URL-safe characters,
  `GOOGLE_TOKEN_ENCRYPTION_KEY` a Fernet key.
- **Compose files are chosen in `.env`** through `COMPOSE_FILE` and `COMPOSE_PATH_SEPARATOR`
  (`:` on Unix, `;` on Windows). Plain `docker compose ps`, `logs`, `up -d` and `exec` therefore
  use the same files later, without `-f` options. Example of a trial setup with Garmin:
  `COMPOSE_FILE=docker-compose.yml:docker-compose.dev.yml:docker-compose.garmin.yml`.
  The installer manages only `docker-compose.yml`, `docker-compose.dev.yml`,
  `docker-compose.garmin.yml` and `docker-compose.apple.yml`. An older `COMPOSE_FILE` entry for
  `docker-compose.windows.yml` or `docker-compose.linux.example.yml`, two empty compatibility files
  that are no longer in the repository, is dropped. Every other entry already in `COMPOSE_FILE`
  (your own files) is kept in its original order. If
  `COMPOSE_FILE` was not set and a `docker-compose.override.yml` exists, it is included, because
  Compose would have loaded it implicitly.
- **Never:** `docker compose down -v`, removing a volume, deleting a backup, deleting any file.
  Running the installer again is safe.
- The access token is printed once at the end, on an interactive terminal only. Without one (a
  pipe, a log file, CI) the summary prints `grep APP_UI_TOKEN <checkout>/.env` instead, and in
  GitHub Actions the value is masked in the log. It stays in `.env` as `APP_UI_TOKEN`.

## Platforms

All platforms run the same `docker-compose.yml`, the same images (amd64 and arm64) and the same
`.env`. Only the host jobs differ:

| Platform | Backup schedule | Start after reboot | Umay bridge service |
| --- | --- | --- | --- |
| Linux / Raspberry Pi / VPS | systemd timer | Docker service enabled; containers `restart: unless-stopped` | systemd unit |
| NAS | the NAS's own task scheduler (by hand) | the NAS's Docker app; containers `restart: unless-stopped` | not supported |
| macOS | launchd agent | Docker Desktop / OrbStack / Colima at login | launchd agent |
| Windows | Task Scheduler task | Docker Desktop at sign-in | scheduled task |

### Linux server or VM (default)

**Requirements:** a 64-bit Debian, Ubuntu, Fedora or similar distribution; Docker Engine with the
Compose plugin ([Docker's install guide](https://docs.docker.com/engine/install/)); Git; `age` and
Python 3.11+ for backups. 2 GB RAM is enough for the stack; disk grows with your archive.
Proxmox: a VM, or an LXC container with nesting enabled for Docker.

```sh
sudo apt install git age python3      # Fedora: sudo dnf install git age python3
sudo systemctl enable --now docker
docker compose version
curl -fsSL https://raw.githubusercontent.com/M3rgen/HealthOS/v0.2.2.1/install.sh | bash
```

**Root or your own user.**

- **As root** (`sudo bash` instead of `bash` in the one-liner, or `sudo bash install.sh`): checkout
  in `/opt/healthos`, units in `/etc/systemd/system/`, backup key `/etc/healthos/backup.key`.
  This matches the [deployment guide](DEPLOYMENT.md).
- **As your own user** (the user must be able to run `docker`, i.e. be in the `docker` group):
  checkout in `~/healthos`, backup key `~/.config/healthos/backup.key`. Interactively the installer
  offers to stop here so you can re-run it with `sudo` for system units instead. The units are installed as
  systemd **user** units (`~/.config/systemd/user/`, managed with `systemctl --user`), and
  lingering is turned on (`loginctl enable-linger`) so they run without an open login session.

**systemd units** (from [`deploy/systemd/`](../deploy/systemd/), with the checkout path filled in;
as user units, `/etc/healthos` becomes `~/.config/healthos`):

| Unit | What it does |
| --- | --- |
| `healthos-backup.timer` / `healthos-backup.service` | Runs `scripts/backup.sh` daily at 03:30 UTC, catching up after downtime. |
| `healthos-backup-failure.service` | Logs a failed backup at `crit` (`journalctl -p crit -t healthos-backup`). |
| `healthos-bridge.service` | Optional Umay bridge; only with `--umay yes`. |

```sh
systemctl list-timers healthos-backup.timer
sudo systemctl start healthos-backup.service && journalctl -u healthos-backup.service -n 20
systemctl status healthos-bridge.service            # if installed
# Installed as your own user: the same commands with --user, without sudo, e.g.
systemctl --user list-timers healthos-backup.timer
journalctl --user -u healthos-backup.service -n 20
```

Autostart on Linux needs no unit of its own: the installer enables the Docker service (as root;
otherwise it tells you to run `sudo systemctl enable docker`), and the containers come back with
`restart: unless-stopped`.

**Without systemd** (for example Alpine or an LXC container without systemd) the installer
installs no units; it creates the backup key and prints a `crontab` line for the daily backup and
a hint for starting Docker at boot.

For the rest of a production server (webhook tunnel, firewall for the bridge, final checklist),
continue with the [deployment guide](DEPLOYMENT.md) from step 5.

#### Open the dashboard from another computer

The app listens only on `127.0.0.1` of the server; this does not change. From your own computer:

```sh
ssh -N -L 8000:127.0.0.1:8000 <user>@<server>
```

Then open <http://localhost:8000> (or <http://healthos.localhost:8000>, see
[deployment step 5](DEPLOYMENT.md#5-open-the-dashboard-through-ssh)). Windows 10/11 ships the same
`ssh` command.

### Raspberry Pi and other ARM64 boards

- **64-bit OS required:** Raspberry Pi OS (64-bit) or Ubuntu Server for ARM64. Every image
  HealthOS uses is built for amd64 and arm64; 32-bit ARM (armv7) is not supported, because Node.js
  and `uv` publish no armv7 build. The installer refuses a 32-bit system (exit code 2); check with
  `uname -m`, which must print `aarch64`. A Pi 3 or newer can run a 64-bit OS.
- **RAM:** 4 GB or more recommended (Pi 4 or Pi 5). 2 GB can run the stack but the first build
  is likely to run out of memory; add swap if you try.
- **First build is slow:** the frontend and Python dependencies are built on the board; expect
  15–30 minutes on a Pi 4. Later updates reuse the cached layers.
- **Storage:** PostgreSQL writes constantly, which wears out SD cards and is slow on them. Put
  Docker's data directory (and with it the `postgres_data` volume) on a USB 3 SSD, or boot the
  Pi from the SSD. Keep the backups on a different disk than the database.
- Install Docker with [Docker's Debian instructions](https://docs.docker.com/engine/install/debian/)
  (or `curl -fsSL https://get.docker.com | sh`), then run the Linux one-liner. The installer
  preselects Linux; choose *Raspberry Pi* or pass `--platform raspberrypi`.

Everything else (systemd units, SSH tunnel, backups) is the same as on
[Linux](#linux-server-or-vm-default).

### NAS (Synology, QNAP, Unraid, TrueNAS SCALE)

A NAS runs the same Compose files and the same `.env`; only the way you start them differs. The
installer's NAS choice prepares the checkout and `.env` and prints the remaining steps; it does not
install services on the NAS.

**Requirements:** an x86-64 or ARM64 NAS with a Docker/Compose app, SSH access, and enough RAM
for building the image (2 GB free). Keep the project on a regular data volume, not on a USB disk.

**Step 1: put the checkout and `.env` on the NAS.** Choose one:

- *With SSH and Git on the NAS* (most models after enabling SSH): log in over SSH, `cd` into a
  shared folder (for example `/volume1/docker` on Synology, `/share/Container` on QNAP,
  `/mnt/user/appdata` on Unraid, a dataset under `/mnt/<pool>` on TrueNAS) and run
  `./install.sh --platform nas --no-start` from a fresh clone, or the one-liner with
  `--dir <that folder>/healthos`.
- *Without Git on the NAS:* on another computer, clone the repository and run
  `./install.sh --platform nas --dry-run --env-out ./nas.env`. Copy the checkout to the NAS
  share and save `nas.env` there as `.env`. Its secrets are real; file paths in it (for example a
  Garmin key file) refer to the computer it ran on, so adjust them for the NAS.

**Step 2: start the project.**

- **Synology DSM 7.2+ (Container Manager):** *Project* › *Create* › choose the checkout folder as
  the path and *Use existing docker-compose.yml*. Container Manager builds the app image and starts
  `app` and `postgres`.
- **QNAP (Container Station 3):** *Applications* › *Create*, paste or upload `docker-compose.yml`
  with the checkout folder as the working directory.
- **Unraid:** install the *Docker Compose Manager* plugin, add a stack whose directory is the
  checkout, then *Compose Up*.
- **TrueNAS SCALE 24.10+:** Apps run on Docker; *Discover Apps* › *Custom App* › *Install via
  YAML* takes a Compose file, but building from the checkout is easier over SSH:
  `sudo docker compose up -d --build` in the checkout.

The NAS interfaces usually read only `docker-compose.yml` and ignore the `COMPOSE_FILE` line of
`.env`. For trial mode (`docker-compose.dev.yml`) or Garmin (`docker-compose.garmin.yml`), start
the stack over SSH instead, from the checkout, where `docker compose up -d --build` honours
`COMPOSE_FILE` (on Synology and QNAP run it with `sudo`). Both paths use the same volume.

**Step 3: backups.** `scripts/backup.sh` needs bash, Python 3.11+ and `age` (or `gpg`) on the NAS.
Many NAS systems ship Python but not `age`; place the static `age` binary from its
[official release](https://github.com/FiloSottile/age/releases) on the `PATH`. Create the key
once as root and copy it off the NAS:

```sh
sudo sh -c 'umask 077; mkdir -p /root/.config/healthos && age-keygen -o /root/.config/healthos/backup.key'
```

Then schedule this command daily at 03:30 (the NAS's local time; the Linux timer uses UTC), as root, with the NAS's own scheduler:

```sh
cd <checkout> && BACKUP_KEY_FILE=/root/.config/healthos/backup.key /bin/bash scripts/backup.sh
```

Synology: *Control Panel* › *Task Scheduler* › *User-defined script*, user root; QNAP:
`crontab -e` over SSH; Unraid: *User Scripts* plugin; TrueNAS: *System* › *Advanced* › *Cron Jobs*.
Set `BACKUP_HOST_DIR` in `.env` to
a folder on a different volume or a share that is itself backed up.

**Step 4: dashboard.** As on Linux, the app is bound to `127.0.0.1` of the NAS; open it through
an [SSH tunnel](#open-the-dashboard-from-another-computer). Do not publish port 8000 to the LAN.

### macOS

**Requirements:** macOS on Apple Silicon or Intel; one Docker runtime: [Docker
Desktop](https://docs.docker.com/desktop/setup/install/mac-install/), [OrbStack](https://orbstack.dev/)
or [Colima](https://github.com/abiosoft/colima) (`brew install colima docker docker-compose`);
Git (`xcode-select --install` or Homebrew); for backups `brew install age` and Python 3.11+
(`brew install python`; the `python3` of the Xcode command line tools is too old).

```sh
curl -fsSL https://raw.githubusercontent.com/M3rgen/HealthOS/v0.2.2.1/install.sh | bash
```

The installer detects macOS and preselects it. Checkout: `~/healthos`. Backup key:
`~/Library/Application Support/HealthOS/backup.key`.

- **Backups and the bridge** run as launchd agents of your user: `org.healthos.backup` (daily at
  03:30 local time, not UTC) and `org.healthos.bridge` (optional), installed in `~/Library/LaunchAgents/` from the
  templates in `deploy/launchd/`. List them with `launchctl list | grep -i healthos`; run a backup
  now with `launchctl kickstart gui/$(id -u)/org.healthos.backup`. Logs: `~/Library/Logs/HealthOS/`.
- **Autostart:** Docker Desktop: *Settings* › *General* › *Start Docker Desktop when you sign in*.
  OrbStack starts at login by default. Colima: `brew services start colima`. The containers then
  come back on their own.
- **Sleep:** a sleeping Mac neither synchronizes nor backs up. launchd runs a missed 03:30 (local time, not UTC) backup
  after waking, but not for a Mac that was switched off. For a Mac that should run HealthOS around
  the clock, set *System Settings* › *Energy* to prevent automatic sleep.

### Windows 10 and 11

**Requirements:** Windows 10 22H2 or Windows 11; [Docker
Desktop](https://docs.docker.com/desktop/setup/install/windows-install/) with the WSL 2 backend
(the default; `wsl --install` sets up WSL 2); [Git for Windows](https://git-scm.com/download/win)
or `winget install Git.Git`; `winget install FiloSottile.age` for backups. Windows PowerShell 5.1
and PowerShell 7 both work.

```powershell
irm https://raw.githubusercontent.com/M3rgen/HealthOS/v0.2.2.1/install.ps1 | iex
```

The installer detects Windows and preselects it. It lists every platform, but installs only the
Windows one on this PC; for another choice it prints the command to run on that machine. Checkout:
`%USERPROFILE%\healthos`. Backup key: `%APPDATA%\HealthOS\backup.key`, readable only by your
account. If Git or Docker Desktop is missing or the Docker engine is not running, it prints the
`winget` command or the fix and stops with exit code 2.

- **Backups** run as the Task Scheduler task `\HealthOS\HealthOS Backup` of your user, daily at
  03:30 local time (not UTC), with `scripts\backup.ps1` (same behaviour as `scripts/backup.sh`). List the tasks with
  `Get-ScheduledTask -TaskPath '\HealthOS\'`; run a backup now with
  `Start-ScheduledTask -TaskPath '\HealthOS\' -TaskName 'HealthOS Backup'`. Logs:
  `%LOCALAPPDATA%\HealthOS\logs\`.
- **Autostart:** Docker Desktop starts only after you sign in. The installer runs
  [`scripts/enable-docker-desktop-autostart.ps1`](../scripts/enable-docker-desktop-autostart.ps1),
  which adds Docker Desktop to your user's startup entries; the containers then come back with
  `restart: unless-stopped`. Until someone signs in, HealthOS is not running.
- **Umay bridge** (optional) runs as the scheduled task `\HealthOS\HealthOS Umay bridge` at
  sign-in; see
  [Windows notes](legacy/WINDOWS.md#umay-bridge-on-windows) for the Codex CLI setup.
- **Running a local copy:** if PowerShell refuses `.\install.ps1` because of the execution
  policy, run it as `powershell -ExecutionPolicy Bypass -File .\install.ps1`. This changes no
  setting.
- Keep the checkout on a local NTFS drive, not in a OneDrive-synced folder.

### Cloud VPS

A VPS is the [Linux](#linux-server-or-vm-default) setup on a rented server; choose *Cloud VPS* or
`--platform vps`. The difference is that the server is on the internet:

- The app is bound to `127.0.0.1` and PostgreSQL publishes no port; keep it that way. Do not open
  port 8000 in the provider's firewall. Reach the dashboard only through an
  [SSH tunnel](#open-the-dashboard-from-another-computer).
- Allow inbound SSH only (preferably key-only); everything else stays closed.
- For Google webhooks, use the webhook-only [Cloudflare Tunnel](CLOUDFLARE_TUNNEL.md): it needs no
  inbound port either.
- Store the backup key and copies of the backups somewhere other than the VPS.

## Data sources

Choose any combination. To add one later, run the installer again with the new `--sources`: it
fills the source's settings, turns its `…_ENABLED` switch on and adds its Compose file to
`COMPOSE_FILE`. Nothing a source already stored is ever removed.

| Source | Devices | What you need | Default |
| --- | --- | --- | --- |
| Google Health API v4 | Fitbit, Pixel Watch, Google Health app | Eligible Google Cloud project, OAuth web client | off until configured |
| Garmin Connect Personal API | Garmin watches and devices | Personal API key file | off (not in `HEALTHOS_SOURCES`) |
| Apple Health | Apple Watch, iPhone | Nothing for `export.zip`; Health Auto Export app for automatic upload | upload available; receiver off |
| None | — | Nothing; manual entries and the empty dashboard | — |

### Google Health API v4 (Fitbit, Pixel Watch)

As of October 3, 2026 Google says new Google Health API projects are not being onboarded; check
the [official setup guide](https://developers.google.com/health/setup) first.

1. Create a **Web application** OAuth client with the redirect URI the installer prints:
   `http://localhost:8000/auth/google/callback` (it works through the SSH tunnel too).
2. Give the installer the client ID and secret (or put them in `.env` later). It generates
   `GOOGLE_TOKEN_ENCRYPTION_KEY` itself. Keep that key with your backup key: a restored database
   needs it to read the stored Google tokens.
3. Open <http://localhost:8000/auth/google/start> and complete consent, then:

   ```sh
   docker compose exec app healthos probe --days 30
   docker compose exec app healthos backfill
   ```

Full mode additionally needs `GOOGLE_HEALTH_WEBHOOK_ENDPOINT_URI` (HTTPS) and
`GOOGLE_HEALTH_WEBHOOK_AUTHORIZATION`; see [deployment step 7](DEPLOYMENT.md#7-webhook-through-cloudflare-tunnel).
Scopes and the seven-day Testing-mode token limit: [OAuth reference](REFERENCE.md#oauth-and-google-health-setup).

### Garmin Connect Personal API

A read-only, pull-only source for your own Garmin Connect account.

1. Create a Personal API key in Garmin Connect and save it as one line in a file (default
   `~/.config/garmin/connect-personal-api-key`), or paste it when the installer asks and let it
   write the file:

   ```sh
   mkdir -p ~/.config/garmin && chmod 700 ~/.config/garmin
   ( umask 077; cat > ~/.config/garmin/connect-personal-api-key )   # paste the key, then Ctrl-D
   ```

2. Choose Garmin in the installer (or `--sources garmin --garmin-key-file PATH`). It sets
   `garmin` in `HEALTHOS_SOURCES` (the only source switch it writes; an existing older
   `GARMIN_ENABLED` line is left as it is and ignored once `HEALTHOS_SOURCES` is set),
   `GARMIN_API_KEY_HOST_FILE` and `GARMIN_API_KEY_HOST_GID`, and adds
   `docker-compose.garmin.yml` to `COMPOSE_FILE`; that override mounts the key file read-only into
   the app. Because the app container runs as its own user (uid 10001), on Linux the installer
   makes the file readable by it. As root it gives the file to group 10001 (or to the
   `GARMIN_API_KEY_HOST_GID` already in `.env`) with mode 640, but only when no other account on the
   host is in that group, neither as a listed member nor as its primary group: a host group that
   happens to have the container's number is not trusted for that reason alone. Otherwise the file
   keeps its permissions, Garmin stays off, and the installer explains how to give the container a
   group of its own (create a group no account belongs to, set the `*_HOST_GID` key to it, re-run).
   If `chgrp` or `chmod` fails, the installer stops with exit code 1 instead of reporting success,
   and the mode is changed only after the group was. As a normal
   user whose primary group is shared (a group id below 1000 such as `users`, or a group with other
   members) it does **not** loosen the file; it prints the exact command to run
   (`sudo chgrp 10001 FILE && sudo chmod 640 FILE`) and leaves Garmin off (the flag stays
   untouched) until you have run it and re-run the installer. With a private primary group it
   makes the file group-readable (mode 640) and adds that group to the container. The key
   itself never goes into `.env`, the database or a log. If the file does not exist, the installer
   leaves Garmin off and says what to do next; save the key there and run it again with
   `--sources garmin`. If you pass `--garmin-key-file PATH` explicitly and the file does not exist,
   the installer stops with exit code 2 instead.
   On macOS with Colima, the VM may not be able to read a file that is only readable by your
   user; if the app cannot read the key or token file, check the file's permissions and Colima's mounts
   (`colima start --mount`), or switch to Docker Desktop or OrbStack. The same rules apply to the Apple
   receiver token file. On Windows the default path is
   `%USERPROFILE%\.config\garmin\connect-personal-api-key`.
3. Check: `docker compose exec app healthos garmin-status`. Earlier history:
   `docker compose exec app healthos garmin-backfill --since 2026-06-01`.

The key expires after a limited period; to renew it, replace the file's content. Details:
[Garmin reference](REFERENCE.md#garmin-connect-personal-api).

### Apple Watch / Apple Health

- **`export.zip` upload needs no setup.** On the iPhone: Health › profile picture › *Export All
  Health Data*; move `export.zip` to the computer you open the dashboard on and upload it on the
  System page (“Apple Sağlık içe aktar”). A newer export later adds only the new records.
- **Health Auto Export receiver (optional, off by default).** Choose yes for the receiver (or
  `--apple-receiver yes`). The installer sets `APPLE_RECEIVER_ENABLED=true`, creates the receiver
  token in `~/.config/healthos/apple-receiver-token` (Windows:
  `%USERPROFILE%\.config\healthos\apple-receiver-token`; an existing file is kept, never rotated),
  mounts it read-only through `docker-compose.apple.yml` and prints the
  `Authorization: Bearer …` header once for the app's REST API automation. Expose only
  `POST /webhooks/apple-health` through your own HTTPS tunnel and set
  `APPLE_RECEIVER_ENDPOINT_URI` to that URL (the installer asks for it). See the
  [README](../README.md#optional-apple-watch--apple-health) and the
  [Apple reference](REFERENCE.md#apple-health-github-version).

### None for now

Choose none (`--sources none`) to start with the empty dashboard and manual entries. Add a source
later as described [above](#data-sources).

## Backups

Every platform uses the same backup semantics as [`scripts/backup.sh`](../scripts/backup.sh): a
`pg_dump` of the database, encrypted with `age` before it touches the disk, written to
`BACKUP_HOST_DIR` (default `./backups` in the checkout) as `health-<UTC stamp>.dump.age`.

- **Kept forever.** Nothing is deleted unless you set `BACKUP_RETENTION_DAYS` to a positive number
  of days yourself. A run refuses to start when the disk is short of space; it never deletes old
  backups to make room.
- **Status:** each run writes `last-backup-status.json`; the dashboard reads it through
  `GET /health/backup`.
- **The key** is an `age` identity outside the checkout and outside `.env`. The installer offers to
  create it:

  | Platform | Key file | Scheduler | Run one backup now |
  | --- | --- | --- | --- |
  | Linux (root), Raspberry Pi, VPS | `/etc/healthos/backup.key` (root, mode 600) | systemd timer | `sudo systemctl start healthos-backup.service` |
  | Linux (own user) | `~/.config/healthos/backup.key` (mode 600) | systemd user timer | `systemctl --user start healthos-backup.service` |
  | NAS | `/root/.config/healthos/backup.key` (create with `age-keygen -o`) | NAS task scheduler | `BACKUP_KEY_FILE=/root/.config/healthos/backup.key bash scripts/backup.sh` |
  | macOS | `~/Library/Application Support/HealthOS/backup.key` | launchd agent | `launchctl kickstart gui/$(id -u)/org.healthos.backup` |
  | Windows | `%APPDATA%\HealthOS\backup.key` (only your account) | Task Scheduler | `powershell -ExecutionPolicy Bypass -File .\scripts\backup.ps1` |

- **Keep a copy of the key off the host**, for example in a password manager, together with
  `GOOGLE_TOKEN_ENCRYPTION_KEY`. Without the key every backup is unreadable. A key that exists only
  on the host is lost with the host.
- Stronger option: create the identity on another machine (`age-keygen -o healthos-backup.age`) and
  put only its public key on the host as `BACKUP_AGE_RECIPIENT` (Linux: in
  `/etc/healthos/backup.env`). Then a stolen host cannot read its own backups.
- **Copy the backups off the host too** with whatever you already use; HealthOS ships no off-site
  copy.
- **Prove a backup** with the restore drill, which never touches the live database:
  `make restore FILE=backups/health-<stamp>.dump.age` (Windows:
  `powershell -ExecutionPolicy Bypass -File .\scripts\restore.ps1 backups\health-<stamp>.dump.age`). See the
  [runbook](RUNBOOK.md#backup-verify-and-restore-drill).

## Updating

Updating is the same on every platform: take a backup, check out the new release tag, run the
installer again. A checkout made by the installer sits on a release tag (no branch), so
`git pull` does not apply; use the tag of the release you want (here the next one, v0.2.2.1):

```sh
cd ~/healthos                 # or /opt/healthos, or %USERPROFILE%\healthos
make backup                  # or: bash scripts/backup.sh  (Windows: .\scripts\backup.ps1)
git fetch --tags
git checkout v0.2.2.1          # a checkout on the main branch instead: git pull --ff-only
./install.sh                 # Windows: .\install.ps1
```

Give the same answers (mode, sources) as before, or pass the same flags, because `COMPOSE_FILE`
follows the choices of the latest run.

Running the installer again is safe: it copies `.env` to a dated backup first, keeps every
secret, fills in settings new releases add, refreshes the scheduled jobs and rebuilds the stack; the app
migrates the database on start. Without the installer, `docker compose up -d --build` does the
rebuild. Update outside the 03:30 backup window (UTC on Linux, local time on macOS and Windows). Release notes: [CHANGELOG](../CHANGELOG.md);
migration details: [runbook](RUNBOOK.md#update-the-application).

Without a new `git checkout` the installer keeps the release that is checked out.

### Updating an install from 0.2.2 or earlier (the old name "HealtOS")

Up to 0.2.2 the project was published as "HealtOS", and some names on your machine still use that
spelling. Running the new installer keeps everything working; no data, key or backup is moved or deleted:

- A checkout in `~/healtos` (`%USERPROFILE%\healtos`) stays the default as long as there is no
  `~/healthos`; the installer says so.
- Files in `~/.config/healtos`, `~/Library/Application Support/HealtOS` (macOS) or
  `%APPDATA%\HealtOS` (Windows), such as `backup.key` and `backup.env`, are **copied** to the new
  `healthos` / `HealthOS` directory when missing there (still readable only by you). The originals
  stay; keep your off-host copy of the backup key as it is. Until the copy exists, `backup.sh` and
  `backup.ps1` use the old key path.
- macOS: the `org.healtos.backup` and `org.healtos.bridge` agents are stopped and replaced by
  `org.healthos.*`; the old plists stay in `~/Library/LaunchAgents/` as `*.plist.bak-<UTC stamp>`.
- Windows: the tasks `\HealtOS\HealtOS Backup` and `\HealtOS\HealtOS Umay bridge` are unregistered
  after `\HealthOS\HealthOS Backup` and `\HealthOS\HealthOS Umay bridge` are registered.
- `HEALTOS_REPO_URL` still works for `install.sh` (with a note); use `HEALTHOS_REPO_URL`.
- New logs go to `~/Library/Logs/HealthOS` and `%LOCALAPPDATA%\HealthOS\logs`; the old log
  folders stay as they are.

## Stopping and uninstalling without losing data

Your archive lives in the Docker volume `postgres_data`, your backups in `BACKUP_HOST_DIR`. Neither
is removed by any step below.

```sh
docker compose stop          # stop; `docker compose start` resumes
docker compose down          # remove the containers and network; the volume stays
```

**Never run `docker compose down -v` or `docker volume rm`:** they delete the database volume and
with it every record that is not in a backup. The installer never does either.

To remove HealthOS from a host while keeping the data:

1. Take a final backup and copy it, the backup key and `.env` off the host.
2. `docker compose down` (without `-v`).
3. Remove the scheduled jobs:
   - Linux: `sudo systemctl disable --now healthos-backup.timer healthos-bridge.service`
     (user units: `systemctl --user disable --now …`).
   - macOS: `launchctl bootout gui/$(id -u)/org.healthos.backup` (and `org.healthos.bridge`); the
     `.plist` files in `~/Library/LaunchAgents/` can stay.
   - Windows: `Disable-ScheduledTask -TaskPath '\HealthOS\' -TaskName 'HealthOS Backup'` (and
     `'HealthOS Umay bridge'`), or *Disable* in Task Scheduler.
4. Leave the checkout, `.env`, the `.env` copies (`~/.local/state/healthos/env-backups/`, older `.env.bak-*` next to `.env`) and
   the `backups` folder where they are, or
   move them to an archive. The volume `postgres_data` stays in Docker until you decide otherwise.

Re-running the installer in the same checkout later picks up the same `.env` and the same volume.

## Troubleshooting

**Every platform**

| Symptom | Cause and fix |
| --- | --- |
| `required variable POSTGRES_PASSWORD is missing` | `.env` is not in the directory where you run `docker compose`; `cd` into the checkout. |
| App restarts with `unsafe or missing production configuration: <names>` | Full mode without all production values. Fill the names into `.env` and run the installer again, or use `--mode trial` until then. |
| Login page rejects the token | Use `APP_UI_TOKEN` from `.env` exactly, without quotes. |
| Port 8000 already in use | Set `APP_PORT` in `.env` and run `docker compose up -d`. Google's redirect URI keeps port 8000, so prefer freeing the port when you use Google. |
| `/health` lists `configuration_incomplete` | Expected until Google is linked and the webhook values are set. |
| Backup status `failed` | Read `last-backup-status.json` in `BACKUP_HOST_DIR`; the usual causes are a missing key, `age` not installed or too little free disk space. |
| Something looks wrong after an update | `docker compose logs --tail=200 app`. The previous `.env` is in `~/.local/state/healthos/env-backups/` (Windows: `%LOCALAPPDATA%\HealthOS\env-backups\`). |

**Linux, Raspberry Pi, VPS**

- `permission denied … docker.sock`: add your user to the `docker` group
  (`sudo usermod -aG docker "$USER"`, then log in again) or run the installer as root.
- Backup timer missing: `systemctl list-timers | grep healthos` (user units:
  `systemctl --user list-timers`); check `journalctl -u healthos-backup.service -n 50`.
- User units stop when you log out: lingering is off; run `loginctl enable-linger "$USER"`.
- Raspberry Pi build killed (`exit code 137`): out of memory; add swap or use a board with 4 GB+.
- `exec format error`: a 32-bit OS or an image built for another architecture; use a 64-bit OS and
  rebuild with `docker compose build --no-cache app`.

**NAS**

- The UI project starts in production mode and stops: the UI ignored `COMPOSE_FILE`; start the
  stack over SSH as described in [step 2](#nas-synology-qnap-unraid-truenas-scale).
- `docker: permission denied` over SSH on Synology/QNAP: use `sudo docker compose …`.

**macOS**

- `Cannot connect to the Docker daemon`: start Docker Desktop, OrbStack or `colima start`.
- Colima runs out of memory during the build: `colima start --memory 4`.
- A launchd agent does not run: `launchctl print gui/$(id -u)/org.healthos.backup` shows its state
  and last exit code; the log path is in `~/Library/LaunchAgents/org.healthos.backup.plist`.

**Windows**

- `docker` is not recognised: open a new PowerShell window after installing Docker Desktop, and
  start Docker Desktop once.
- WSL 2 errors in Docker Desktop: run `wsl --update` as administrator and restart.
- Script refused by the execution policy: use `powershell -ExecutionPolicy Bypass -File .\install.ps1`.
- Backup task present but nothing happens: open the task's *History* tab in Task Scheduler, or run
  `.\scripts\backup.ps1` by hand to see the error.

More: [technical reference](REFERENCE.md), [runbook](RUNBOOK.md),
[deployment guide](DEPLOYMENT.md).
