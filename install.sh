#!/usr/bin/env bash
# HealtOS installer for Linux servers (default), Raspberry Pi, NAS, cloud VPS and macOS.
# Windows: use install.ps1. English / Türkçe. Run `bash install.sh --help` for every option.
#
#   curl -fsSL https://raw.githubusercontent.com/M3rgen/HealtOS/main/install.sh | bash
#   bash install.sh                       # inside a checkout: guided setup
#   bash install.sh --dry-run             # show every step, write only <dir>/.env.dry-run
#
# Safety: an existing .env is first copied to .env.bak-<UTC stamp>; secrets and other values
# are never rotated or changed, only missing or empty keys are filled. The exceptions are the
# selections themselves: COMPOSE_FILE follows the chosen mode and sources (a source still
# switched on in .env keeps its key mount), a chosen source is switched on (never off; that is
# only reported), and an explicit --garmin-key-file replaces the Garmin key path. Nothing is ever deleted (no `docker compose down -v`, no volume, backup or file
# removal). Re-running is safe. Exit codes: 0 ok, 1 a step failed, 2 bad arguments or an
# unsupported environment.
#
# Runs on bash 3.2 (macOS) and newer with POSIX tools, curl or wget, git (to clone), and
# openssl or /dev/urandom for secrets. No Python is needed for the installer itself.
HEALTOS_INSTALLER_VERSION="0.2.2"

set -u
set -o pipefail

REPO_URL="${HEALTOS_REPO_URL:-https://github.com/M3rgen/HealtOS.git}"
RAW_URL="https://raw.githubusercontent.com/M3rgen/HealtOS"
APP_UID=10001
# Python for the Umay bridge's .venv: the newest Python a host offers (Homebrew already ships
# 3.15) can be newer than every wheel in uv.lock (psycopg-binary, websockets: cp312-cp314), so
# uv sync picks or downloads one in this range. Keep in step with install.ps1,
# deploy/windows/start-umay-bridge.ps1 and the installer job in .github/workflows/ci.yml.
BRIDGE_PYTHON='>=3.12,<3.15'

# ---- options (shared contract with install.ps1) -------------------------------------
LANG_SEL=""
PLATFORM=""
MODE=""
SOURCES=""
DIR=""
REF="main"
GARMIN_KEY_FILE=""
GOOGLE_CLIENT_ID_IN=""
GOOGLE_CLIENT_SECRET_IN=""
APPLE_RECEIVER=""
BACKUPS=""
AUTOSTART=""
UMAY=""
NONINTERACTIVE=0
DRY_RUN=0
NO_START=0
ENV_OUT=""
WEBHOOK_HOST_IN=""
APPLE_ENDPOINT_IN=""

# ---- state ---------------------------------------------------------------------------
INTERACTIVE=0
HOST_OS=""
HOST_ARCH=""
DETECTED=""
IS_ROOT=0
NEED_CLONE=0
SCRIPT_DIR=""
ENV_RESULT=""
ENV_FRESH=0
FILL=""
NOTES=""
MISSING=""
STARTED=0
START_SKIPPED=""
UNIT_MODE=""
APPLE_TOKEN_NEW=""
APPLE_TOKEN_FILE=""
APPLE_BLOCKED=0
GARMIN_PASTED=0
GARMIN_KEY_EXPLICIT=0
BACKUP_KEY_PATH=""
BACKUP_PUBKEY=""
C_B=""
C_Y=""
C_R=""
C_G=""
C_0=""

# ---- output --------------------------------------------------------------------------
t() { if [ "$LANG_SEL" = tr ]; then printf '%s' "$2"; else printf '%s' "$1"; fi; }
say() { printf '%s\n' "$(t "$1" "$2")"; }
step() { printf '\n%s== %s ==%s\n' "$C_B" "$(t "$1" "$2")" "$C_0"; }
ok() { printf '  %s✓%s %s\n' "$C_G" "$C_0" "$(t "$1" "$2")"; }
warn() { printf '  %s!%s %s\n' "$C_Y" "$C_0" "$(t "$1" "$2")"; }
fail_line() { printf '  %s✗%s %s\n' "$C_R" "$C_0" "$(t "$1" "$2")"; }
die() {
  local code="$1"
  shift
  printf '%sERROR / HATA:%s %s\n' "$C_R" "$C_0" "$(t "$1" "$2")" >&2
  exit "$code"
}
note() { NOTES="${NOTES}$(t "$1" "$2")
"; }
missing() { MISSING="${MISSING}$(t "$1" "$2")
"; }

setup_colors() {
  if [ -t 1 ] && [ -z "${NO_COLOR:-}" ] && [ "${TERM:-dumb}" != dumb ]; then
    C_B=$'\033[1m'
    C_Y=$'\033[33m'
    C_R=$'\033[31m'
    C_G=$'\033[32m'
    C_0=$'\033[0m'
  fi
}

have() {
  local found
  found="$(command -v "$1" 2>/dev/null)" || return 1
  # macOS: /usr/bin/git and /usr/bin/python3 are stubs that open an installer dialog
  # until the Command Line Tools exist; treat them as missing instead of running them.
  if [ "$HOST_OS" = Darwin ]; then
    case "$found" in
      /usr/bin/git | /usr/bin/python3) xcode-select -p >/dev/null 2>&1 || return 1 ;;
    esac
  fi
  return 0
}

# Every changing action goes through run: printed, and skipped in --dry-run.
run() {
  if [ "$DRY_RUN" = 1 ]; then
    printf '  [dry-run] %s\n' "$*"
    return 0
  fi
  printf '  $ %s\n' "$*"
  "$@"
}

usage() {
  cat <<'EOF'
HealtOS installer (Linux default, Raspberry Pi, NAS, macOS, cloud VPS). Windows: install.ps1.
HealtOS kurulum betiği (varsayılan Linux; Raspberry Pi, NAS, macOS, bulut VPS). Windows: install.ps1.

Usage / Kullanım:  bash install.sh [options / seçenekler]

  --lang en|tr                 language / dil (interactive default: ask, English preselected)
  --platform P                 linux (default) | raspberrypi | nas | macos | windows | vps
  --mode trial|full            trial (default): local look, no Google needed / yerel deneme
                               full: production; needs Google + webhook / üretim; Google gerekli
  --sources LIST               google,garmin,apple or none / veri kaynakları (virgüllü)
  --dir PATH                   checkout directory / kurulum dizini (default: current checkout,
                               else /opt/healthos as root or ~/healtos)
  --ref REF                    git ref to clone / klonlanacak sürüm (default main)
  --garmin-key-file PATH       Garmin Connect Personal API key file / Garmin anahtar dosyası
  --google-client-id ID        Google OAuth client id
  --google-client-secret S     Google OAuth client secret
  --apple-receiver yes|no      Health Auto Export receiver / alıcı (default no)
  --backups yes|no             daily encrypted backup / günlük şifreli yedek
                               (default yes in full, no in trial)
  --autostart yes|no           start after reboot / yeniden başlatmada aç (default yes)
  --umay yes|no                Umay bridge service / Umay köprü servisi (default no)
  --yes, --non-interactive     never prompt; flags and defaults only / soru sormadan
  --dry-run                    print every step; write only --env-out / yalnız göster
  --env-out PATH               dry-run .env output (default <dir>/.env.dry-run)
  --no-start                   configure only, do not start containers / yalnız yapılandır
  -h, --help                   this help / bu yardım

Exit codes / Çıkış kodları: 0 ok, 1 a step failed / bir adım başarısız,
  2 bad arguments or unsupported environment / hatalı argüman veya desteklenmeyen ortam.

Secrets in an existing .env are never changed; COMPOSE_FILE follows the chosen mode and sources.
A .env.bak-<stamp> copy is made before any change. / Var olan .env'deki gizli değerler asla
değiştirilmez; COMPOSE_FILE seçilen kip ve kaynaklara uyar. Her değişiklikten önce .env.bak-<damga>
kopyası alınır.
EOF
}

# ---- arguments ----------------------------------------------------------------------
bad_arg() {
  printf 'install.sh: %s\n' "$1" >&2
  printf 'install.sh: see / bakınız: bash install.sh --help\n' >&2
  exit 2
}

need_value() {
  [ "$2" -ge 2 ] || bad_arg "$1 needs a value / $1 bir değer bekler"
}

yes_no() {
  case "$2" in
    yes | no) ;;
    *) bad_arg "$1 must be yes or no / $1 yes ya da no olmalı (got '$2')" ;;
  esac
}

parse_args() {
  local opt val
  while [ "$#" -gt 0 ]; do
    opt="$1"
    case "$opt" in
      --*=*)
        val="${opt#*=}"
        opt="${opt%%=*}"
        shift
        set -- "$opt" "$val" "$@"
        continue
        ;;
    esac
    case "$opt" in
      -h | --help)
        usage
        exit 0
        ;;
      --yes | -y | --non-interactive) NONINTERACTIVE=1 ;;
      --dry-run) DRY_RUN=1 ;;
      --no-start) NO_START=1 ;;
      --lang | --platform | --mode | --sources | --dir | --ref | --garmin-key-file | \
        --google-client-id | --google-client-secret | --apple-receiver | --backups | \
        --autostart | --umay | --env-out)
        need_value "$opt" "$#"
        val="$2"
        shift
        case "$opt" in
          --lang) LANG_SEL="$val" ;;
          --platform) PLATFORM="$val" ;;
          --mode) MODE="$val" ;;
          --sources) SOURCES="$val" ;;
          --dir) DIR="$val" ;;
          --ref) REF="$val" ;;
          --garmin-key-file)
            GARMIN_KEY_FILE="$val"
            GARMIN_KEY_EXPLICIT=1
            ;;
          --google-client-id) GOOGLE_CLIENT_ID_IN="$val" ;;
          --google-client-secret) GOOGLE_CLIENT_SECRET_IN="$val" ;;
          --apple-receiver) APPLE_RECEIVER="$val" ;;
          --backups) BACKUPS="$val" ;;
          --autostart) AUTOSTART="$val" ;;
          --umay) UMAY="$val" ;;
          --env-out) ENV_OUT="$val" ;;
        esac
        ;;
      *) bad_arg "unknown option / bilinmeyen seçenek: $opt" ;;
    esac
    shift
  done
  case "$LANG_SEL" in '' | en | tr) ;; *) bad_arg "--lang must be en or tr / --lang en ya da tr olmalı" ;; esac
  case "$PLATFORM" in
    '' | linux | raspberrypi | nas | macos | windows | vps) ;;
    *) bad_arg "--platform must be linux, raspberrypi, nas, macos, windows or vps" ;;
  esac
  case "$MODE" in '' | trial | full) ;; *) bad_arg "--mode must be trial or full / --mode trial ya da full olmalı" ;; esac
  [ -z "$APPLE_RECEIVER" ] || yes_no --apple-receiver "$APPLE_RECEIVER"
  [ -z "$BACKUPS" ] || yes_no --backups "$BACKUPS"
  [ -z "$AUTOSTART" ] || yes_no --autostart "$AUTOSTART"
  [ -z "$UMAY" ] || yes_no --umay "$UMAY"
  [ -n "$REF" ] || bad_arg "--ref must not be empty / --ref boş olamaz"
  case "$REF" in -*) bad_arg "--ref must not start with '-'" ;; esac
  [ -z "$ENV_OUT" ] || [ "$DRY_RUN" = 1 ] \
    || bad_arg "--env-out only works with --dry-run / --env-out yalnız --dry-run ile çalışır"
  if [ -n "$SOURCES" ]; then
    SOURCES="$(normalize_sources "$SOURCES")" || bad_arg "--sources takes google, garmin, apple (comma list) or none / --sources google, garmin, apple ya da none alır"
  fi
  if [ "$GARMIN_KEY_EXPLICIT" = 1 ]; then
    # An explicit flag is honoured or fails; only the default key path may be missing.
    GARMIN_KEY_FILE="$(abs_path "$GARMIN_KEY_FILE")"
    [ -f "$GARMIN_KEY_FILE" ] || bad_arg "--garmin-key-file $GARMIN_KEY_FILE not found: save your Garmin Personal API key there (one line, mode 600) or leave the flag out; nothing was changed. / --garmin-key-file $GARMIN_KEY_FILE bulunamadı: Garmin Personal API anahtarınızı oraya kaydedin (tek satır, izin 600) ya da seçeneği kaldırın; hiçbir şey değiştirilmedi."
  fi
}

normalize_sources() {
  # Prints a canonical comma list (google,garmin,apple order) or "none"; fails on bad input.
  local raw item out="" g=0 m=0 a=0 n=0
  raw="$(printf '%s' "$1" | tr 'A-Z ' 'a-z,')"
  local IFS=,
  for item in $raw; do
    case "$item" in
      '') ;;
      google) g=1 ;;
      garmin) m=1 ;;
      apple) a=1 ;;
      none) n=1 ;;
      *) return 1 ;;
    esac
  done
  [ "$g$m$a" = 000 ] || [ "$n" = 0 ] || return 1
  [ "$g" = 0 ] || out="${out},google"
  [ "$m" = 0 ] || out="${out},garmin"
  [ "$a" = 0 ] || out="${out},apple"
  out="${out#,}"
  printf '%s' "${out:-none}"
}

has_source() { case ",$SOURCES," in *",$1,"*) return 0 ;; esac; return 1; }

# ---- prompts (from /dev/tty, so `curl ... | bash` works) -----------------------------
ask() {
  # ask VAR DEFAULT "question en" "question tr"
  local answer
  if [ -n "$2" ]; then
    printf '%s [%s]: ' "$(t "$3" "$4")" "$2" >&3
  else
    printf '%s: ' "$(t "$3" "$4")" >&3
  fi
  IFS= read -r answer <&3 || answer=""
  [ -n "$answer" ] || answer="$2"
  printf -v "$1" '%s' "$answer"
}

ask_secret() {
  local answer
  printf '%s: ' "$(t "$2" "$3")" >&3
  IFS= read -r -s answer <&3 || answer=""
  printf '\n' >&3
  printf -v "$1" '%s' "$answer"
}

ask_yes_no() {
  # ask_yes_no VAR DEFAULT(yes|no) "question en" "question tr"
  local answer hint
  if [ "$2" = yes ]; then hint="$(t 'Y/n' 'E/h')"; else hint="$(t 'y/N' 'e/H')"; fi
  while :; do
    printf '%s [%s]: ' "$(t "$3" "$4")" "$hint" >&3
    IFS= read -r answer <&3 || answer=""
    case "$(printf '%s' "$answer" | tr '[:upper:]' '[:lower:]')" in
      '') answer="$2" ;;
      y | yes | e | evet) answer=yes ;;
      n | no | h | hayir | hayır) answer=no ;;
      *)
        say "Please answer yes or no." "Lütfen evet ya da hayır yanıtlayın."
        continue
        ;;
    esac
    printf -v "$1" '%s' "$answer"
    return 0
  done
}

menu() {
  # menu VAR DEFAULT_INDEX "title en" "title tr" "item en" "item tr" ...
  local var="$1" def="$2" answer i=1 n mark
  printf '\n%s%s%s\n' "$C_B" "$(t "$3" "$4")" "$C_0"
  shift 4
  while [ "$#" -ge 2 ]; do
    mark=""
    [ "$i" != "$def" ] || mark="  <- $(t 'default' 'varsayılan')"
    printf '  %d) %s%s\n' "$i" "$(t "$1" "$2")" "$mark"
    i=$((i + 1))
    shift 2
  done
  n=$((i - 1))
  while :; do
    printf '%s [%s]: ' "$(t 'Choose a number' 'Bir numara seçin')" "$def" >&3
    IFS= read -r answer <&3 || answer=""
    [ -n "$answer" ] || answer="$def"
    case "$answer" in
      *[!0-9]*) ;;
      *)
        if [ "$answer" -ge 1 ] && [ "$answer" -le "$n" ]; then
          printf -v "$var" '%s' "$answer"
          return 0
        fi
        ;;
    esac
    say "Please enter a number from 1 to $n." "Lütfen 1 ile $n arasında bir sayı girin."
  done
}

# ---- host detection ------------------------------------------------------------------
detect_host() {
  HOST_OS="$(uname -s 2>/dev/null || echo unknown)"
  HOST_ARCH="$(uname -m 2>/dev/null || echo unknown)"
  [ "$(id -u)" != 0 ] || IS_ROOT=1
  case "$HOST_OS" in
    Darwin) DETECTED=macos ;;
    Linux)
      DETECTED=linux
      if [ -r /proc/device-tree/model ] && grep -qa 'Raspberry Pi' /proc/device-tree/model 2>/dev/null; then
        DETECTED=raspberrypi
      elif [ -f /etc/synoinfo.conf ] || [ -f /etc/config/qpkg.conf ] || [ -f /etc/unraid-version ] \
        || { [ -r /etc/version ] && grep -qi truenas /etc/version 2>/dev/null; }; then
        DETECTED=nas
      fi
      ;;
    MINGW* | MSYS* | CYGWIN*) DETECTED=windows ;;
    *) DETECTED=unknown ;;
  esac
}

host_label() {
  case "$DETECTED" in
    macos) printf 'macOS' ;;
    windows) printf 'Windows' ;;
    raspberrypi) printf 'Raspberry Pi (Linux)' ;;
    nas) printf 'NAS (Linux)' ;;
    linux) printf 'Linux' ;;
    *) printf '%s' "$HOST_OS" ;;
  esac
}

platform_index() {
  case "$1" in
    linux) echo 1 ;; raspberrypi) echo 2 ;; nas) echo 3 ;; macos) echo 4 ;; windows) echo 5 ;; vps) echo 6 ;; *) echo 1 ;;
  esac
}

platform_from_index() {
  case "$1" in
    1) echo linux ;; 2) echo raspberrypi ;; 3) echo nas ;; 4) echo macos ;; 5) echo windows ;; 6) echo vps ;;
  esac
}

is_linux_family() { case "$PLATFORM" in linux | raspberrypi | nas | vps) return 0 ;; esac; return 1; }

# ---- checkout and directory ----------------------------------------------------------
is_checkout() {
  [ -f "$1/docker-compose.yml" ] && [ -f "$1/.env.example" ] && [ -f "$1/scripts/backup.sh" ]
}

abs_path() {
  # Absolute form of a path that may not exist yet; ~ expanded.
  local p="$1"
  # shellcheck disable=SC2088 # a literal ~ typed by the user is expanded here on purpose
  case "$p" in "~") p="$HOME" ;; "~/"*) p="$HOME/${p#\~/}" ;; esac
  case "$p" in /*) ;; *) p="$PWD/$p" ;; esac
  if [ -d "$p" ]; then (cd "$p" && pwd -P); else printf '%s' "$p"; fi
}

# ---- dry-run preview target ----------------------------------------------------------
PREVIEW_MARKER="# install.sh --dry-run: the .env it would write. Not used by Docker."
PREVIEW_MARKER_PS1="# install.ps1 -DryRun: the .env it would write. Not used by Docker."

is_preview() {
  # An earlier dry-run preview (either installer) starts with its marker line.
  local first=""
  [ -f "$1" ] && [ ! -L "$1" ] || return 1
  IFS= read -r first <"$1" 2>/dev/null || [ -n "$first" ] || return 1
  first="${first%$'\r'}"
  [ "$first" = "$PREVIEW_MARKER" ] || [ "$first" = "$PREVIEW_MARKER_PS1" ]
}

resolve_env_out() {
  # --env-out never replaces a real file: its directory is resolved (symlinks too), then the
  # checkout's .env, any .env.bak-* and any existing file that is not an earlier preview are
  # refused (exit 2) before anything is written.
  local p="$ENV_OUT" d b
  [ -n "$p" ] || p="$DIR/.env.dry-run"
  # shellcheck disable=SC2088 # a literal ~ typed by the user is expanded here on purpose
  case "$p" in "~") p="$HOME" ;; "~/"*) p="$HOME/${p#\~/}" ;; esac
  case "$p" in /*) ;; *) p="$PWD/$p" ;; esac
  while :; do case "$p" in */) p="${p%/}" ;; *) break ;; esac; done
  b="${p##*/}"
  d="${p%/*}"
  [ -n "$d" ] || d=/
  case "$b" in '' | . | ..)
    die 2 "--env-out must name a file: $ENV_OUT" "--env-out bir dosya adı olmalı: $ENV_OUT" ;;
  esac
  d="$(cd -P "$d" 2>/dev/null && pwd -P)" \
    || die 2 "The directory for --env-out does not exist: ${p%/*}" "--env-out dizini yok: ${p%/*}"
  ENV_OUT="${d%/}/$b"
  [ "$ENV_OUT" != "$DIR/.env" ] || die 2 "--env-out must not be the real .env." "--env-out gerçek .env olamaz."
  case "$b" in .env.bak-*)
    die 2 "--env-out must not be a .env.bak-* copy: $ENV_OUT" "--env-out bir .env.bak-* kopyası olamaz: $ENV_OUT" ;;
  esac
  if { [ -e "$ENV_OUT" ] || [ -L "$ENV_OUT" ]; } && ! is_preview "$ENV_OUT"; then
    die 2 "$ENV_OUT exists and is not an earlier dry-run preview; it is not overwritten. Choose another --env-out." \
      "$ENV_OUT var ve önceki bir deneme önizlemesi değil; üzerine yazılmaz. Başka bir --env-out seçin."
  fi
}

default_dir() {
  if [ "$HOST_OS" = Linux ] && [ "$IS_ROOT" = 1 ]; then
    printf '/opt/healthos'
  else
    printf '%s/healtos' "$HOME"
  fi
}

resolve_dir() {
  local top
  if [ -z "$DIR" ]; then
    if [ -n "$SCRIPT_DIR" ] && is_checkout "$SCRIPT_DIR"; then
      DIR="$SCRIPT_DIR"
    elif is_checkout "$PWD"; then
      DIR="$(pwd -P)"
    elif have git && top="$(git rev-parse --show-toplevel 2>/dev/null)" && is_checkout "$top"; then
      DIR="$top"
    else
      DIR="$(default_dir)"
      if [ "$INTERACTIVE" = 1 ] && ! is_checkout "$DIR"; then
        ask DIR "$DIR" "Install directory (HealtOS will be cloned here)" "Kurulum dizini (HealtOS buraya klonlanır)"
      fi
    fi
  fi
  DIR="$(abs_path "$DIR")"
  if is_checkout "$DIR"; then
    NEED_CLONE=0
  elif [ ! -e "$DIR" ] || { [ -d "$DIR" ] && [ -z "$(ls -A "$DIR" 2>/dev/null)" ]; }; then
    NEED_CLONE=1
  else
    die 2 "$DIR exists but is not a HealtOS checkout and not empty. Choose another --dir; nothing was changed." \
      "$DIR var ama bir HealtOS kopyası değil ve boş değil. Başka bir --dir seçin; hiçbir şey değiştirilmedi."
  fi
}

# ---- secrets (openssl, else /dev/urandom) --------------------------------------------
rand_b64() {
  if have openssl; then
    openssl rand -base64 "$1" | tr -d '\n'
  elif [ -r /dev/urandom ] && have base64; then
    head -c "$1" /dev/urandom | base64 | tr -d '\n'
  else
    return 1
  fi
}

rand_hex() {
  if have openssl; then
    openssl rand -hex "$1"
  elif [ -r /dev/urandom ] && have od; then
    od -An -tx1 -N"$1" /dev/urandom | tr -d ' \n'
  else
    return 1
  fi
}

gen_secret() {
  # gen_secret hex64|token|fernet|bearer
  local value
  case "$1" in
    hex64) value="$(rand_hex 32)" ;;
    token) value="$(rand_b64 32 | tr '+/' '-_' | tr -d '=')" ;;
    fernet) value="$(rand_b64 32 | tr '+/' '-_')" ;;
    bearer) value="Bearer $(rand_b64 32 | tr '+/' '-_' | tr -d '=')" ;;
  esac
  [ -n "$value" ] || die 1 "Cannot generate secrets: install openssl (or provide /dev/urandom and base64)." \
    "Gizli değer üretilemedi: openssl kurun (ya da /dev/urandom ve base64 sağlayın)."
  printf '%s' "$value"
}

# ---- .env ----------------------------------------------------------------------------
env_get() {
  # Last KEY=value (or `export KEY=value`) of a file, quotes stripped, never sourced (same
  # rule as backup.sh).
  [ -f "$1" ] || return 0
  sed -n "s/^[[:space:]]*\(export[[:space:]][[:space:]]*\)\{0,1\}$2[[:space:]]*=[[:space:]]*//p" "$1" \
    | tail -n 1 | sed "s/^[\"']//; s/[\"']$//"
}

ENV_SRC=""

fill() {
  FILL="${FILL}$1	$2
"
}

lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }

want() {
  # A non-secret selection (COMPOSE_FILE): follows the current choices. A changed existing
  # value is reported; the previous .env is kept as .env.bak-<stamp>.
  local key="$1" value="$2" current
  current="$(env_get "$ENV_SRC" "$key")"
  [ "$current" != "$value" ] || return 0
  fill "$key" "$value"
  if [ "$ENV_FRESH" = 0 ] && [ -n "$current" ]; then
    note "$key changed from '$current' to '$value' (previous .env kept as .env.bak-<stamp>)." \
      "$key '$current' değerinden '$value' değerine değişti (önceki .env, .env.bak-<damga> olarak duruyor)."
  fi
}

is_off() {
  # The one rule for enable flags (same in install.ps1): empty or false/0/no/off (any case)
  # is off; every other value counts as on.
  case "$(lower "$1")" in '' | false | 0 | no | off) return 0 ;; esac
  return 1
}

want_on() {
  # Turn a source on: only an off value becomes true; any other value is left as it is.
  # Turning one off is never done silently.
  is_off "$(env_get "$ENV_SRC" "$1")" && fill "$1" true
  return 0
}

still_on() {
  # still_on ENABLED_KEY HOST_FILE_KEY: true when an existing .env keeps a source switched on
  # with its host file set, although it was not chosen this time.
  ! is_off "$(env_get "$DIR/.env" "$1")" && [ -n "$(env_get "$DIR/.env" "$2")" ]
}

note_if_on() {
  # note_if_on KEY HOST_FILE_KEY NAME: the source is not selected now but still enabled in .env.
  [ "$ENV_FRESH" = 0 ] && ! is_off "$(env_get "$ENV_SRC" "$1")" || return 0
  if [ -n "$(env_get "$ENV_SRC" "$2")" ]; then
    note "$3 was not selected but $1 is on in .env: it stays on and its key mount stays in COMPOSE_FILE. Set $1=false by hand to turn it off." \
      "$3 seçilmedi ama .env içinde $1 açık: açık kalır ve anahtar bağlaması COMPOSE_FILE'da kalır. Kapatmak için $1=false yapın."
  else
    note "$1 is on in .env but $2 is empty, so no key is mounted for $3. Set $1=false, or re-run with $3 selected." \
      ".env içinde $1 açık ama $2 boş, bu yüzden $3 için anahtar bağlanmıyor. $1=false yapın ya da $3 seçerek yeniden çalıştırın."
  fi
}

want_path() {
  # A key-file path or its group: filled when empty; replaced only for an explicit flag.
  local current
  current="$(env_get "$ENV_SRC" "$1")"
  if [ -z "$current" ] || { [ "$3" = 1 ] && [ "$current" != "$2" ]; }; then
    want "$1" "$2"
  fi
}

want_secret() {
  # A secret: generated only when missing or empty; an existing one is never rotated.
  local current
  current="$(env_get "$ENV_SRC" "$1")"
  [ -n "$current" ] || fill "$1" "$(gen_secret "$2")"
}

want_given() {
  # A value the user typed: filled when empty; never replaces an existing one.
  local current
  [ -n "$2" ] || return 0
  current="$(env_get "$ENV_SRC" "$1")"
  if [ -z "$current" ]; then
    fill "$1" "$2"
  elif [ "$current" != "$2" ]; then
    note "$1 already has a value in .env; the new one was not written." \
      "$1 için .env içinde zaten bir değer var; yenisi yazılmadı."
  fi
}

compose_entries() {
  # The existing .env's COMPOSE_FILE, one entry per line (its own COMPOSE_PATH_SEPARATOR).
  local current sep entry
  current="$(env_get "$DIR/.env" COMPOSE_FILE)"
  [ -n "$current" ] || return 0
  sep="$(env_get "$DIR/.env" COMPOSE_PATH_SEPARATOR)"
  [ -n "$sep" ] || sep=":"
  current="$current$sep"
  while [ -n "$current" ]; do
    entry="${current%%"$sep"*}"
    current="${current#*"$sep"}"
    [ -z "$entry" ] || printf '%s\n' "$entry"
  done
}

is_managed_compose() {
  case "${1#./}" in
    docker-compose.yml | docker-compose.dev.yml | docker-compose.garmin.yml | docker-compose.apple.yml | \
      docker-compose.windows.yml) return 0 ;;
  esac
  return 1
}

compose_files() {
  # The installer's files for the chosen mode and sources, then every other file the existing
  # COMPOSE_FILE names, in its order. Without a COMPOSE_FILE, docker-compose.override.yml
  # (which Compose loaded implicitly) is kept by naming it.
  local files="docker-compose.yml" entry user
  [ "$MODE" = full ] || files="$files:docker-compose.dev.yml"
  # A source left switched on in .env keeps its mount: never enabled with no key mounted.
  if has_source garmin || still_on GARMIN_ENABLED GARMIN_API_KEY_HOST_FILE; then
    files="$files:docker-compose.garmin.yml"
  fi
  if [ "$APPLE_RECEIVER" = yes ] || still_on APPLE_RECEIVER_ENABLED APPLE_RECEIVER_TOKEN_HOST_FILE; then
    files="$files:docker-compose.apple.yml"
  fi
  user="$(compose_entries)"
  if [ -z "$user" ]; then
    [ ! -f "$DIR/docker-compose.override.yml" ] || files="$files:docker-compose.override.yml"
  else
    while IFS= read -r entry; do
      is_managed_compose "$entry" && continue
      case ":$files:" in *":$entry:"*) continue ;; esac
      files="$files:$entry"
    done <<EOF_ENTRIES
$user
EOF_ENTRIES
  fi
  printf '%s' "$files"
}

default_mode() {
  # Re-run default: an existing COMPOSE_FILE without the development override means full.
  local entries
  entries="$(compose_entries)"
  [ -n "$entries" ] || { printf trial; return 0; }
  case "
$entries
" in
    *"
docker-compose.dev.yml
"* | *"
./docker-compose.dev.yml
"*) printf trial ;;
    *) printf full ;;
  esac
}

# ls -n is the portable (GNU and BSD) way to read numeric owner and group.
# shellcheck disable=SC2012
file_gid() { ls -ldn "$1" 2>/dev/null | awk '{ print $4 }'; }
# shellcheck disable=SC2012
file_owner() { ls -ldn "$1" 2>/dev/null | awk '{ print $3 ":" $4 }'; }

secret_file_gid() {
  # Group that lets the app container (uid 10001) read a host secret file. As root on Linux
  # the file is given to the app's own group (chgrp 10001, mode 640; see share_with_app).
  local gid
  gid="$(file_gid "$1")"
  if [ "$IS_ROOT" = 1 ] && { [ "$HOST_OS" = Linux ] || [ "$gid" = 0 ]; }; then gid="$APP_UID"; fi
  printf '%s' "${gid:-$APP_UID}"
}

gid_is_shared() {
  # True when other accounts can be in group $1: a system/shared gid (< 1000, e.g. `users`
  # 100), listed members other than this user, or another account with it as primary group.
  local gid="$1" me line members m accounts
  case "$gid" in '' | *[!0-9]*) return 0 ;; esac
  [ "$gid" -ge 1000 ] || return 0
  me="$(id -un 2>/dev/null)"
  if have getent; then
    line="$(getent group "$gid" 2>/dev/null | head -n 1)"
    accounts="$(getent passwd 2>/dev/null)"
  else
    line="$(grep "^[^:]*:[^:]*:$gid:" /etc/group 2>/dev/null | head -n 1)"
    accounts="$(cat /etc/passwd 2>/dev/null)"
  fi
  members="$(printf '%s' "$line" | cut -d: -f4)"
  for m in $(printf '%s' "$members" | tr ',' ' '); do
    [ "$m" = "$me" ] || return 0
  done
  printf '%s\n' "$accounts" | awk -F: -v g="$gid" -v me="$me" '$4 == g && $1 != me { found = 1 } END { exit !found }'
}

BLOCK_GID=""
secret_blocked() {
  # Linux, not root: the app container could read FILE only if it were made readable by a
  # group other accounts share. Then nothing is widened and the source stays off (D8).
  local f="$1"
  [ "$HOST_OS" = Linux ] && [ "$IS_ROOT" = 0 ] || return 1
  if [ -e "$f" ]; then
    [ -z "$(find "$f" -prune -perm -040 2>/dev/null)" ] || return 1
    BLOCK_GID="$(file_gid "$f")"
  else
    BLOCK_GID="$(id -g)"
  fi
  gid_is_shared "$BLOCK_GID"
}

block_note() {
  # block_note NAME_EN NAME_TR FILE RERUN_FLAGS
  warn "$1 stays off: $3 would have to be readable by group $BLOCK_GID, which other accounts share." \
    "$2 kapalı kalıyor: $3 dosyasının $BLOCK_GID grubunca okunabilmesi gerekirdi; bu grubu başka hesaplar da kullanıyor."
  note "$1 is off: $3 was not made readable by the shared group $BLOCK_GID. Give it to the app container's group, then re-run the installer with $4 (and your other choices): sudo chgrp $APP_UID $3 && sudo chmod 640 $3" \
    "$2 kapalı: $3 ortak $BLOCK_GID grubuna okunur yapılmadı. Dosyayı uygulama konteynerinin grubuna verin, sonra kurulumu $4 (ve diğer seçimlerinizle) yeniden çalıştırın: sudo chgrp $APP_UID $3 && sudo chmod 640 $3"
}

plan_env() {
  local example="$DIR/.env.example" target="$DIR/.env" garmin apple_file
  FILL=""
  if [ -f "$target" ]; then
    ENV_SRC="$target"
    ENV_FRESH=0
  else
    ENV_SRC="$example"
    ENV_FRESH=1
  fi
  want_secret POSTGRES_PASSWORD hex64
  want_secret APP_UI_TOKEN token
  want COMPOSE_FILE "$(compose_files)"
  want COMPOSE_PATH_SEPARATOR ":"
  if has_source google || [ "$MODE" = full ]; then
    want_secret GOOGLE_TOKEN_ENCRYPTION_KEY fernet
    want_given GOOGLE_CLIENT_ID "$GOOGLE_CLIENT_ID_IN"
    want_given GOOGLE_CLIENT_SECRET "$GOOGLE_CLIENT_SECRET_IN"
  fi
  if [ "$MODE" = full ]; then
    want_secret GOOGLE_HEALTH_WEBHOOK_AUTHORIZATION bearer
    [ -z "$WEBHOOK_HOST_IN" ] || want_given GOOGLE_HEALTH_WEBHOOK_ENDPOINT_URI "https://$WEBHOOK_HOST_IN/webhooks/google-health"
  fi
  if has_source garmin; then
    garmin="$(abs_path "$GARMIN_KEY_FILE")"
    want_on GARMIN_ENABLED
    want_path GARMIN_API_KEY_HOST_FILE "$garmin" "$GARMIN_KEY_EXPLICIT"
    want_path GARMIN_API_KEY_HOST_GID "$(secret_file_gid "$garmin")" "$GARMIN_KEY_EXPLICIT"
  fi
  has_source garmin || note_if_on GARMIN_ENABLED GARMIN_API_KEY_HOST_FILE Garmin
  [ "$APPLE_RECEIVER" = yes ] || note_if_on APPLE_RECEIVER_ENABLED APPLE_RECEIVER_TOKEN_HOST_FILE "Apple receiver"
  if [ "$APPLE_RECEIVER" = yes ]; then
    apple_file="$(apple_token_path)"
    want_on APPLE_RECEIVER_ENABLED
    want_path APPLE_RECEIVER_TOKEN_HOST_FILE "$apple_file" 0
    if [ -f "$apple_file" ]; then
      want_path APPLE_RECEIVER_TOKEN_HOST_GID "$(secret_file_gid "$apple_file")" 0
    else
      want_path APPLE_RECEIVER_TOKEN_HOST_GID "$(new_secret_gid)" 0
    fi
    want_given APPLE_RECEIVER_ENDPOINT_URI "$APPLE_ENDPOINT_IN"
  fi
  if [ "$UMAY" = yes ]; then
    want_secret CODEX_BRIDGE_TOKEN token
  fi
}

new_secret_gid() {
  if [ "$IS_ROOT" = 1 ]; then printf '%s' "$APP_UID"; else id -g; fi
}

apple_token_path() {
  local current
  current="$(env_get "$DIR/.env" APPLE_RECEIVER_TOKEN_HOST_FILE)"
  if [ -n "$current" ]; then abs_path "$current"; else printf '%s/.config/healthos/apple-receiver-token' "$HOME"; fi
}

render_env() {
  # render_env SOURCE DEST new|update|preview: SOURCE with FILL applied (last assignment of a
  # key replaced, `export KEY=` kept as such; missing keys appended). awk reads values from
  # the environment: no escape processing. new: DEST must not exist (never clobbered);
  # update: the real .env, already copied to .env.bak-*; preview: an earlier preview only.
  local src="$1" dest="$2" kind="$3" tmp header first=""
  header="# Added by install.sh $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  [ "$kind" != preview ] || first="$PREVIEW_MARKER"
  tmp="$(mktemp "$dest.tmp.XXXXXXXX")" || die 1 "Cannot write next to $dest." "$dest yanına yazılamıyor."
  chmod 600 "$tmp"
  if ! HEALTOS_FILL="$FILL" HEALTOS_FILL_HEADER="$header" HEALTOS_FIRST="$first" awk '
    BEGIN {
      n = split(ENVIRON["HEALTOS_FILL"], rows, "\n")
      for (i = 1; i <= n; i++) {
        if (rows[i] == "") continue
        tab = index(rows[i], "\t")
        key = substr(rows[i], 1, tab - 1)
        if (!(key in val)) order[++m] = key
        val[key] = substr(rows[i], tab + 1)
      }
    }
    {
      line[NR] = $0
      if ($0 ~ /^[ \t]*#/) next
      eq = index($0, "=")
      if (eq == 0) next
      key = substr($0, 1, eq - 1)
      gsub(/^[ \t]+|[ \t]+$/, "", key)
      lead = ""
      if (key ~ /^export[ \t]/) { lead = "export "; sub(/^export[ \t]+/, "", key) }
      if (key in val) { last[key] = NR; prefix[key] = lead }
    }
    END {
      if (ENVIRON["HEALTOS_FIRST"] != "") print ENVIRON["HEALTOS_FIRST"]
      for (key in last) line[last[key]] = prefix[key] key "=" val[key]
      for (i = 1; i <= NR; i++) print line[i]
      added = 0
      for (j = 1; j <= m; j++) {
        key = order[j]
        if (key in last) continue
        if (!added) { print ""; print ENVIRON["HEALTOS_FILL_HEADER"]; added = 1 }
        print key "=" val[key]
      }
    }' "$src" >"$tmp"; then
    rm -f "$tmp"
    die 1 "Could not render $dest; nothing was changed." "$dest oluşturulamadı; hiçbir şey değiştirilmedi."
  fi
  case "$kind" in
    new)
      # A hard link never replaces an existing file; mv only when links are unsupported.
      if ln "$tmp" "$dest" 2>/dev/null; then
        rm -f "$tmp"
      elif [ -e "$dest" ] || [ -L "$dest" ] || ! mv "$tmp" "$dest"; then
        rm -f "$tmp"
        die 1 "Could not create $dest (it may exist already); nothing was changed." \
          "$dest oluşturulamadı (zaten var olabilir); hiçbir şey değiştirilmedi."
      fi
      ;;
    preview)
      if { [ -e "$dest" ] || [ -L "$dest" ]; } && ! is_preview "$dest"; then
        rm -f "$tmp"
        die 2 "$dest exists and is not an earlier dry-run preview; it is not overwritten. Choose another --env-out." \
          "$dest var ve önceki bir deneme önizlemesi değil; üzerine yazılmaz. Başka bir --env-out seçin."
      fi
      mv -f "$tmp" "$dest" || { rm -f "$tmp"; die 1 "Could not write $dest." "$dest yazılamadı."; }
      ;;
    *)
      mv -f "$tmp" "$dest" || { rm -f "$tmp"; die 1 "Could not write $dest." "$dest yazılamadı."; }
      ;;
  esac
}

backup_env() {
  local stamp bak n=1
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  bak="$DIR/.env.bak-$stamp"
  while [ -e "$bak" ]; do
    n=$((n + 1))
    bak="$DIR/.env.bak-$stamp-$n"
  done
  cp -p "$DIR/.env" "$bak" || die 1 "Could not copy .env to $bak; nothing was changed." \
    ".env, $bak dosyasına kopyalanamadı; hiçbir şey değiştirilmedi."
  chmod 600 "$bak"
  ok "Copied the existing .env to $bak (kept forever)." "Var olan .env $bak olarak kopyalandı (silinmez)."
}

write_env() {
  local target="$DIR/.env" owner
  step "Configuration (.env)" "Yapılandırma (.env)"
  plan_env
  if [ "$DRY_RUN" = 1 ]; then
    resolve_env_out
    render_env "$ENV_SRC" "$ENV_OUT" preview
    ENV_RESULT="$ENV_OUT"
    if [ "$ENV_FRESH" = 1 ]; then
      say "  [dry-run] would create $target from .env.example (mode 600)" "  [dry-run] .env.example'dan $target oluşturulurdu (izin 600)"
    elif [ -n "$FILL" ]; then
      say "  [dry-run] would copy .env to .env.bak-<UTC stamp>, then fill missing keys and update the selections" \
        "  [dry-run] .env, .env.bak-<UTC damga> olarak kopyalanır, sonra eksik anahtarlar doldurulup seçimler güncellenirdi"
    fi
    ok "Preview written to $ENV_OUT (the real .env is untouched)." "Önizleme $ENV_OUT dosyasına yazıldı (gerçek .env'e dokunulmadı)."
  elif [ "$ENV_FRESH" = 1 ]; then
    (umask 077 && render_env "$DIR/.env.example" "$target" new) || exit 1
    if [ "$IS_ROOT" = 1 ]; then
      owner="$(file_owner "$DIR")"
      [ "$owner" = "0:0" ] || chown "$owner" "$target" 2>/dev/null || true
    fi
    ENV_RESULT="$target"
    ok "Created .env from .env.example (mode 600)." ".env, .env.example'dan oluşturuldu (izin 600)."
  elif [ -n "$FILL" ]; then
    owner="$(file_owner "$target")"
    backup_env
    render_env "$target" "$target" update
    [ "$IS_ROOT" != 1 ] || chown "$owner" "$target" 2>/dev/null || true
    ENV_RESULT="$target"
    ok "Filled missing keys and updated the selections; no secret was changed." \
      "Eksik anahtarlar dolduruldu, seçimler güncellendi; hiçbir gizli değer değiştirilmedi."
  else
    ENV_RESULT="$target"
    ok ".env already complete; not modified." ".env zaten tam; değiştirilmedi."
  fi
  if [ -n "$FILL" ]; then
    printf '  %s %s\n' "$(t 'Keys set:' 'Ayarlanan anahtarlar:')" "$(printf '%s' "$FILL" | cut -f1 | tr '\n' ' ')"
  fi
}

# ---- preflight -----------------------------------------------------------------------
PREFLIGHT_FATAL=0

preflight_fatal() {
  fail_line "$1" "$2"
  PREFLIGHT_FATAL=1
}

docker_install_hint() {
  case "$PLATFORM" in
    macos)
      say "    Install one of: Docker Desktop (https://docs.docker.com/desktop/setup/install/mac-install/)," \
        "    Birini kurun: Docker Desktop (https://docs.docker.com/desktop/setup/install/mac-install/),"
      say "    OrbStack (brew install --cask orbstack) or Colima (brew install colima docker docker-compose && colima start)." \
        "    OrbStack (brew install --cask orbstack) ya da Colima (brew install colima docker docker-compose && colima start)."
      ;;
    nas)
      say "    Synology: Package Center > Container Manager. QNAP: App Center > Container Station." \
        "    Synology: Paket Merkezi > Container Manager. QNAP: App Center > Container Station."
      say "    Unraid: Docker is built in (add the Compose Manager plugin). TrueNAS SCALE: Apps (Docker)." \
        "    Unraid: Docker yerleşik (Compose Manager eklentisini ekleyin). TrueNAS SCALE: Apps (Docker)."
      ;;
    *)
      say "    Debian/Ubuntu/Raspberry Pi OS: curl -fsSL https://get.docker.com | sudo sh   (or https://docs.docker.com/engine/install/)" \
        "    Debian/Ubuntu/Raspberry Pi OS: curl -fsSL https://get.docker.com | sudo sh   (ya da https://docs.docker.com/engine/install/)"
      say "    Fedora: sudo dnf install docker-ce docker-compose-plugin (Docker's repo); Arch: sudo pacman -S docker docker-compose" \
        "    Fedora: sudo dnf install docker-ce docker-compose-plugin (Docker deposu); Arch: sudo pacman -S docker docker-compose"
      say "    Then: sudo systemctl enable --now docker" "    Sonra: sudo systemctl enable --now docker"
      ;;
  esac
}

pkg_hint() {
  # pkg_hint TOOL: one line with the install command per OS family.
  case "$PLATFORM" in
    macos) printf 'brew install %s' "$1" ;;
    nas) printf '%s' "$(t "use the NAS package center or a static $1 binary" "NAS paket merkezini ya da statik $1 dosyasını kullanın")" ;;
    *) printf 'sudo apt install %s  |  sudo dnf install %s  |  sudo pacman -S %s  |  sudo apk add %s' "$1" "$1" "$1" "$1" ;;
  esac
}

python_ok() {
  local candidate
  for candidate in python3 python3.13 python3.12 python3.11 "$DIR/.venv/bin/python"; do
    have "$candidate" || continue
    "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))' >/dev/null 2>&1 && return 0
  done
  return 1
}

port_busy() { (exec 3<>"/dev/tcp/127.0.0.1/$1") >/dev/null 2>&1; }

free_kib() {
  local p="$1"
  while [ ! -d "$p" ] && [ "$p" != / ]; do p="$(dirname "$p")"; done
  df -Pk "$p" 2>/dev/null | awk 'END { print $4 }'
}

mem_mib() {
  if [ -r /proc/meminfo ]; then
    awk '/^MemTotal:/ { print int($2 / 1024) }' /proc/meminfo
  elif have sysctl; then
    sysctl -n hw.memsize 2>/dev/null | awk '{ print int($1 / 1048576) }'
  fi
}

docker_runtime_mac() {
  local ctx
  ctx="$(docker context show 2>/dev/null </dev/null || true)"
  case "$ctx" in
    *orbstack*) echo orbstack ;;
    *colima*) echo colima ;;
    desktop-linux) echo desktop ;;
    *)
      if [ -d /Applications/OrbStack.app ]; then
        echo orbstack
      elif [ -d /Applications/Docker.app ]; then
        echo desktop
      elif have colima; then
        echo colima
      else
        echo unknown
      fi
      ;;
  esac
}

preflight() {
  local bits mem kib out need_docker=1
  step "Preflight checks" "Ön kontroller"
  [ "$NO_START" = 0 ] && [ "$DRY_RUN" = 0 ] || need_docker=0

  # CPU architecture: 64-bit only.
  bits="$(getconf LONG_BIT 2>/dev/null || echo 64)"
  case "$HOST_ARCH" in
    armv6* | armv7* | armhf | arm | i386 | i486 | i586 | i686)
      preflight_fatal "32-bit CPU/OS ($HOST_ARCH) is not supported. Install a 64-bit OS (Raspberry Pi OS 64-bit, Ubuntu arm64)." \
        "32 bit işlemci/işletim sistemi ($HOST_ARCH) desteklenmiyor. 64 bit bir işletim sistemi kurun (Raspberry Pi OS 64-bit, Ubuntu arm64)."
      ;;
    *)
      if [ "$bits" = 32 ]; then
        preflight_fatal "This is a 32-bit userland on a 64-bit CPU ($HOST_ARCH). Install the 64-bit OS image." \
          "64 bit işlemcide 32 bit kullanıcı alanı çalışıyor ($HOST_ARCH). 64 bit işletim sistemi imajını kurun."
      else
        ok "CPU architecture: $HOST_ARCH (64-bit)" "İşlemci mimarisi: $HOST_ARCH (64 bit)"
      fi
      ;;
  esac

  # Memory.
  mem="$(mem_mib)"
  if [ -n "$mem" ]; then
    if [ "$mem" -lt 1800 ]; then
      warn "Only ${mem} MiB RAM: the first build may fail; 4 GB or more is recommended." \
        "Yalnız ${mem} MiB RAM: ilk derleme başarısız olabilir; 4 GB ve üzeri önerilir."
    elif [ "$PLATFORM" = raspberrypi ] && [ "$mem" -lt 3800 ]; then
      warn "${mem} MiB RAM: works, but 4 GB or more is recommended on a Raspberry Pi; the first build is slow." \
        "${mem} MiB RAM: çalışır ama Raspberry Pi'de 4 GB ve üzeri önerilir; ilk derleme yavaştır."
    else
      ok "Memory: ${mem} MiB" "Bellek: ${mem} MiB"
    fi
  fi

  # Disk space where the checkout lives.
  kib="$(free_kib "$DIR")"
  case "$kib" in
    '' | *[!0-9]*) warn "Could not read free disk space." "Boş disk alanı okunamadı." ;;
    *)
      if [ "$kib" -lt 2097152 ]; then
        preflight_fatal "Only $((kib / 1048576)) GiB free near $DIR; at least 5 GiB is needed (images, database, backups)." \
          "$DIR çevresinde yalnız $((kib / 1048576)) GiB boş; en az 5 GiB gerekir (imajlar, veritabanı, yedekler)."
      elif [ "$kib" -lt 5242880 ]; then
        warn "$((kib / 1048576)) GiB free: tight. 5 GiB or more is recommended; the archive and backups grow." \
          "$((kib / 1048576)) GiB boş: sınırda. 5 GiB ve üzeri önerilir; arşiv ve yedekler büyür."
      else
        ok "Free disk space: $((kib / 1048576)) GiB" "Boş disk alanı: $((kib / 1048576)) GiB"
      fi
      ;;
  esac

  # git: required only to clone.
  if have git; then
    ok "git: $(git --version 2>/dev/null | head -n 1)" "git: $(git --version 2>/dev/null | head -n 1)"
  elif [ "$NEED_CLONE" = 1 ]; then
    preflight_fatal "git is required to download HealtOS. Install: $(pkg_hint git)" \
      "HealtOS'u indirmek için git gerekir. Kurulum: $(pkg_hint git)"
    [ "$PLATFORM" != macos ] || say "    macOS: xcode-select --install" "    macOS: xcode-select --install"
  else
    warn "git not found: fine for now, needed later for updates ($(pkg_hint git))." \
      "git bulunamadı: şimdilik sorun değil, güncellemeler için gerekir ($(pkg_hint git))."
  fi

  # Secrets need openssl or /dev/urandom.
  if have openssl || { [ -r /dev/urandom ] && have base64 && have od; }; then
    ok "Random source for secrets available." "Gizli değerler için rastgele kaynak mevcut."
  else
    preflight_fatal "Neither openssl nor /dev/urandom+base64 is available. Install: $(pkg_hint openssl)" \
      "Ne openssl ne /dev/urandom+base64 mevcut. Kurulum: $(pkg_hint openssl)"
  fi

  # Docker Engine / Desktop and Compose v2.
  if ! have docker; then
    if [ "$need_docker" = 1 ]; then
      preflight_fatal "Docker is not installed." "Docker kurulu değil."
    else
      warn "Docker is not installed (needed before starting)." "Docker kurulu değil (başlatmadan önce gerekir)."
    fi
    docker_install_hint
  else
    if docker compose version >/dev/null 2>&1 </dev/null; then
      ok "Docker Compose v2: $(docker compose version --short 2>/dev/null </dev/null || echo ok)" \
        "Docker Compose v2: $(docker compose version --short 2>/dev/null </dev/null || echo ok)"
    else
      if have docker-compose; then
        say "    Only the old docker-compose v1 was found; HealtOS needs the 'docker compose' v2 plugin." \
          "    Yalnız eski docker-compose v1 bulundu; HealtOS 'docker compose' v2 eklentisini ister."
      fi
      if [ "$need_docker" = 1 ]; then
        preflight_fatal "Docker Compose v2 ('docker compose') is missing." "Docker Compose v2 ('docker compose') eksik."
      else
        warn "Docker Compose v2 ('docker compose') is missing." "Docker Compose v2 ('docker compose') eksik."
      fi
      case "$PLATFORM" in
        macos) say "    brew install docker-compose (Colima) or update Docker Desktop/OrbStack." "    brew install docker-compose (Colima) ya da Docker Desktop/OrbStack'i güncelleyin." ;;
        nas) say "    Update Container Manager / Container Station, or install the Compose plugin." "    Container Manager / Container Station'ı güncelleyin ya da Compose eklentisini kurun." ;;
        *) say "    sudo apt install docker-compose-plugin  (or: sudo dnf install docker-compose-plugin)" "    sudo apt install docker-compose-plugin  (ya da: sudo dnf install docker-compose-plugin)" ;;
      esac
    fi
    if out="$(docker info --format '{{.ServerVersion}}' 2>&1 </dev/null)"; then
      ok "Docker daemon reachable (server $out)." "Docker servisi erişilebilir (sunucu $out)."
    else
      case "$out" in
        *ermission*)
          say "    Permission denied on the Docker socket. Add your user to the docker group:" \
            "    Docker soketinde izin reddedildi. Kullanıcınızı docker grubuna ekleyin:"
          say "      sudo usermod -aG docker $(id -un)   # then log out and back in (or: newgrp docker)" \
            "      sudo usermod -aG docker $(id -un)   # sonra oturumu kapatıp açın (ya da: newgrp docker)"
          ;;
        *)
          case "$PLATFORM" in
            macos) say "    Start Docker: open -a Docker  |  orb start  |  colima start" "    Docker'ı başlatın: open -a Docker  |  orb start  |  colima start" ;;
            *) say "    Start Docker: sudo systemctl enable --now docker" "    Docker'ı başlatın: sudo systemctl enable --now docker" ;;
          esac
          ;;
      esac
      if [ "$need_docker" = 1 ]; then
        preflight_fatal "Docker is installed but not reachable." "Docker kurulu ama erişilemiyor."
      else
        warn "Docker is installed but not reachable (needed before starting)." "Docker kurulu ama erişilemiyor (başlatmadan önce gerekir)."
      fi
    fi
    if [ "$PLATFORM" = macos ]; then
      case "$(docker_runtime_mac)" in
        desktop) ok "Runtime: Docker Desktop" "Çalışma ortamı: Docker Desktop" ;;
        orbstack) ok "Runtime: OrbStack" "Çalışma ortamı: OrbStack" ;;
        colima) ok "Runtime: Colima" "Çalışma ortamı: Colima" ;;
        *) warn "Docker runtime not recognised (Docker Desktop, OrbStack or Colima expected)." "Docker ortamı tanınmadı (Docker Desktop, OrbStack ya da Colima beklenir)." ;;
      esac
    fi
  fi

  # HTTP client for the health check.
  if ! have curl && ! have wget; then
    warn "Neither curl nor wget found; the health check after start is skipped. Install: $(pkg_hint curl)" \
      "Ne curl ne wget bulundu; başlatma sonrası sağlık kontrolü atlanır. Kurulum: $(pkg_hint curl)"
  fi

  if [ "$PREFLIGHT_FATAL" = 1 ]; then
    if [ "$DRY_RUN" = 1 ]; then
      warn "Dry run: a real run would stop here (exit 2) until the items marked ✗ are fixed." \
        "Deneme: gerçek kurulum ✗ ile işaretli maddeler düzelene dek burada dururdu (çıkış 2)."
    else
      die 2 "Fix the items marked ✗ above and run the installer again; nothing was changed." \
        "Yukarıda ✗ ile işaretli maddeleri düzeltip kurulumu yeniden çalıştırın; hiçbir şey değiştirilmedi."
    fi
  fi
}

# ---- choices -------------------------------------------------------------------------
choose_language() {
  local idx
  [ -z "$LANG_SEL" ] || return 0
  if [ "$INTERACTIVE" = 1 ]; then
    LANG_SEL=en
    menu idx 1 "Language / Dil" "Language / Dil" "English" "English" "Türkçe" "Türkçe"
    if [ "$idx" = 2 ]; then LANG_SEL="tr"; fi
  else
    LANG_SEL=en
  fi
}

choose_platform() {
  local idx def
  if [ -z "$PLATFORM" ]; then
    def=linux
    case "$DETECTED" in macos | raspberrypi | nas | windows) def="$DETECTED" ;; esac
    if [ "$INTERACTIVE" = 1 ]; then
      menu idx "$(platform_index "$def")" "Where will HealtOS run?" "HealtOS nerede çalışacak?" \
        "Linux server / VM (Debian, Ubuntu, Fedora...; Proxmox VM/LXC) - recommended" \
        "Linux sunucu / sanal makine (Debian, Ubuntu, Fedora...; Proxmox VM/LXC) - önerilen" \
        "Raspberry Pi / ARM64 board - 64-bit OS required; 4 GB+ RAM recommended; first build is slow" \
        "Raspberry Pi / ARM64 kart - 64 bit işletim sistemi şart; 4 GB+ RAM önerilir; ilk derleme yavaş" \
        "NAS (Synology Container Manager, QNAP Container Station, Unraid, TrueNAS SCALE) - prints NAS steps" \
        "NAS (Synology Container Manager, QNAP Container Station, Unraid, TrueNAS SCALE) - NAS adımlarını yazar" \
        "macOS (Apple Silicon or Intel) with Docker Desktop, OrbStack or Colima" \
        "macOS (Apple Silicon ya da Intel), Docker Desktop, OrbStack ya da Colima ile" \
        "Windows 10/11 with Docker Desktop (WSL 2) - uses install.ps1" \
        "Windows 10/11, Docker Desktop (WSL 2) ile - install.ps1 kullanılır" \
        "Cloud VPS - like Linux; open the dashboard through an SSH tunnel" \
        "Bulut VPS - Linux gibi; panel SSH tüneliyle açılır"
      PLATFORM="$(platform_from_index "$idx")"
    else
      PLATFORM="$def"
    fi
  fi
  if [ "$PLATFORM" = windows ]; then
    say "Windows uses the PowerShell installer. In PowerShell:" "Windows'ta PowerShell kurulum betiği kullanılır. PowerShell'de:"
    printf '  irm %s/main/install.ps1 | iex\n' "$RAW_URL"
    say "Inside WSL 2 you can instead run this script with --platform linux." "WSL 2 içinde bu betiği --platform linux ile de çalıştırabilirsiniz."
    exit 2
  fi
  local mismatch=0
  if is_linux_family && [ "$HOST_OS" != Linux ]; then mismatch=1; fi
  if [ "$PLATFORM" = macos ] && [ "$HOST_OS" != Darwin ]; then mismatch=1; fi
  if [ "$mismatch" = 1 ]; then
    if [ "$DRY_RUN" = 1 ]; then
      warn "Platform '$PLATFORM' does not match this $(host_label) host: dry-run preview only." \
        "'$PLATFORM' platformu bu $(host_label) bilgisayarına uymuyor: yalnız deneme önizlemesi."
    else
      die 2 "Platform '$PLATFORM' cannot be installed on this $(host_label) host. Run the installer on the target machine (or use --dry-run to preview)." \
        "'$PLATFORM' bu $(host_label) bilgisayarına kurulamaz. Kurulumu hedef makinede çalıştırın (önizleme için --dry-run)."
    fi
  fi
}

choose_mode() {
  local idx def
  [ -z "$MODE" ] || return 0
  # An explicit --mode wins; otherwise a re-run keeps the mode of the existing .env.
  def="$(default_mode)"
  if [ "$INTERACTIVE" = 1 ]; then
    menu idx "$([ "$def" = full ] && echo 2 || echo 1)" "Mode" "Kip" \
      "Trial - look around locally; development override; no Google account needed" \
      "Deneme - yerelde göz atın; geliştirme ayarı; Google hesabı gerekmez" \
      "Full - production mode; needs Google OAuth and webhook values; daily backups" \
      "Tam - üretim kipi; Google OAuth ve webhook değerleri gerekir; günlük yedek"
    if [ "$idx" = 2 ]; then MODE=full; else MODE=trial; fi
  else
    MODE="$def"
  fi
}

choose_sources() {
  local answer item out="" def bad
  if [ -n "$SOURCES" ]; then :
  elif [ -n "$GARMIN_KEY_FILE" ] || [ -n "$GOOGLE_CLIENT_ID_IN" ] || [ "$APPLE_RECEIVER" = yes ]; then
    SOURCES="none"
  elif [ "$INTERACTIVE" = 1 ]; then
    if [ "$MODE" = full ]; then def=1; else def=4; fi
    printf '\n%s%s%s\n' "$C_B" "$(t 'Data sources (wearables) - pick one or more, e.g. 1,3' 'Veri kaynakları (giyilebilirler) - bir ya da daha fazla seçin, örn. 1,3')" "$C_0"
    printf '  1) %s\n' "$(t 'Google Health API v4 (Fitbit, Pixel Watch, Google Health app) - OAuth client; required in full mode' 'Google Health API v4 (Fitbit, Pixel Watch, Google Health uygulaması) - OAuth istemcisi; tam kipte şart')"
    printf '  2) %s\n' "$(t 'Garmin Connect Personal API - read-only pull; needs your Personal API key file' 'Garmin Connect Personal API - salt okunur çekme; Personal API anahtar dosyanız gerekir')"
    printf '  3) %s\n' "$(t 'Apple Watch / Apple Health - export.zip upload works as is; optional Health Auto Export receiver' 'Apple Watch / Apple Sağlık - export.zip yükleme hazır; isteğe bağlı Health Auto Export alıcısı')"
    printf '  4) %s\n' "$(t 'None for now - manual entries, empty dashboard' 'Şimdilik hiçbiri - elle girişler, boş panel')"
    while :; do
      printf '%s [%s]: ' "$(t 'Choose' 'Seçin')" "$def" >&3
      IFS= read -r answer <&3 || answer=""
      [ -n "$answer" ] || answer="$def"
      out=""
      bad=0
      for item in $(printf '%s' "$answer" | tr ',' ' '); do
        case "$item" in
          1) out="$out,google" ;; 2) out="$out,garmin" ;; 3) out="$out,apple" ;; 4) out="$out,none" ;;
          *) bad=1 ;;
        esac
      done
      if [ "$bad" = 0 ] && SOURCES="$(normalize_sources "${out#,}")"; then break; fi
      say "Enter numbers 1-4 separated by commas (4 alone for none)." "1-4 arası numaraları virgülle girin (hiçbiri için yalnız 4)."
    done
  elif [ "$MODE" = full ]; then
    SOURCES=google
  else
    SOURCES=none
  fi
  # Flags imply their source.
  if [ -n "$GARMIN_KEY_FILE" ] && ! has_source garmin; then SOURCES="$(normalize_sources "${SOURCES#none},garmin")"; fi
  if [ -n "$GOOGLE_CLIENT_ID_IN$GOOGLE_CLIENT_SECRET_IN" ] && ! has_source google; then SOURCES="$(normalize_sources "${SOURCES#none},google")"; fi
  if [ "$APPLE_RECEIVER" = yes ] && ! has_source apple; then SOURCES="$(normalize_sources "${SOURCES#none},apple")"; fi
}

source_details() {
  local current
  if has_source google; then
    if [ "$INTERACTIVE" = 1 ]; then
      current="$(env_get "$DIR/.env" GOOGLE_CLIENT_ID)"
      if [ -z "$current" ] && [ -z "$GOOGLE_CLIENT_ID_IN" ]; then
        say "" ""
        say "Google: create an OAuth 'Web application' client in Google Cloud (README step 3) with redirect URI" \
          "Google: Google Cloud'da 'Web uygulaması' OAuth istemcisi oluşturun (README 3. adım); yönlendirme URI'si"
        printf '  %s\n' "http://localhost:8000/auth/google/callback"
        ask GOOGLE_CLIENT_ID_IN "" "Client ID (empty: add later)" "İstemci kimliği (boş: sonra eklenir)"
        if [ -n "$GOOGLE_CLIENT_ID_IN" ] && [ -z "$GOOGLE_CLIENT_SECRET_IN" ]; then
          ask_secret GOOGLE_CLIENT_SECRET_IN "Client secret (hidden)" "İstemci gizli anahtarı (gizli)"
        fi
      fi
    fi
  fi
  if has_source garmin; then
    if [ -z "$GARMIN_KEY_FILE" ]; then
      current="$(env_get "$DIR/.env" GARMIN_API_KEY_HOST_FILE)"
      GARMIN_KEY_FILE="${current:-$HOME/.config/garmin/connect-personal-api-key}"
      if [ "$INTERACTIVE" = 1 ]; then
        ask GARMIN_KEY_FILE "$GARMIN_KEY_FILE" "Garmin Personal API key file" "Garmin Personal API anahtar dosyası"
      fi
    fi
    GARMIN_KEY_FILE="$(abs_path "$GARMIN_KEY_FILE")"
    if [ ! -f "$GARMIN_KEY_FILE" ] && [ "$INTERACTIVE" = 1 ]; then
      garmin_paste_key
    fi
    if has_source garmin && [ ! -f "$GARMIN_KEY_FILE" ] && [ "$GARMIN_PASTED" = 0 ]; then
      drop_garmin
    fi
    if has_source garmin && secret_blocked "$GARMIN_KEY_FILE"; then
      remove_source garmin
      block_note Garmin Garmin "$GARMIN_KEY_FILE" "--sources garmin"
    fi
  fi
  if has_source apple && [ -z "$APPLE_RECEIVER" ]; then
    if [ "$INTERACTIVE" = 1 ]; then
      say "" ""
      say "Apple Health: export.zip uploads (System page) need nothing. The Health Auto Export receiver lets the iOS app" \
        "Apple Sağlık: export.zip yükleme (Sistem sayfası) ayar istemez. Health Auto Export alıcısı iOS uygulamasının"
      say "push data itself; it needs an https address that reaches this machine." \
        "veriyi kendisinin göndermesini sağlar; bu makineye ulaşan bir https adresi gerekir."
      ask_yes_no APPLE_RECEIVER no "Turn on the Health Auto Export receiver?" "Health Auto Export alıcısı açılsın mı?"
    else
      APPLE_RECEIVER=no
    fi
  fi
  [ -n "$APPLE_RECEIVER" ] || APPLE_RECEIVER=no
  if [ "$APPLE_RECEIVER" = yes ]; then
    APPLE_TOKEN_FILE="$(apple_token_path)"
    if secret_blocked "$APPLE_TOKEN_FILE"; then
      APPLE_RECEIVER=no
      APPLE_BLOCKED=1
      block_note "The Apple receiver" "Apple alıcısı" "$APPLE_TOKEN_FILE" "--apple-receiver yes"
    fi
  fi
  if [ "$APPLE_RECEIVER" = yes ] && [ "$INTERACTIVE" = 1 ] && [ -z "$(env_get "$DIR/.env" APPLE_RECEIVER_ENDPOINT_URI)" ]; then
    ask APPLE_ENDPOINT_IN "" "Receiver https URL ending in /webhooks/apple-health (empty: later)" \
      "Alıcının /webhooks/apple-health ile biten https adresi (boş: sonra)"
  fi
  if [ "$MODE" = full ] && [ "$INTERACTIVE" = 1 ] && [ -z "$(env_get "$DIR/.env" GOOGLE_HEALTH_WEBHOOK_ENDPOINT_URI)" ]; then
    ask WEBHOOK_HOST_IN "" "Public webhook host name, e.g. webhook.example.com (empty: later)" \
      "Herkese açık webhook ana makine adı, örn. webhook.example.com (boş: sonra)"
    WEBHOOK_HOST_IN="${WEBHOOK_HOST_IN#https://}"
    WEBHOOK_HOST_IN="${WEBHOOK_HOST_IN%%/*}"
  fi
}

remove_source() {
  SOURCES="$(printf '%s' "$SOURCES" | tr ',' '\n' | grep -v "^$1\$" | tr '\n' ',')"
  SOURCES="$(normalize_sources "${SOURCES%,}")"
}

drop_garmin() {
  # Garmin chosen but no key file: continue with Garmin left off and say what to do.
  remove_source garmin
  warn "Garmin key file not found: $GARMIN_KEY_FILE. Continuing with Garmin off." \
    "Garmin anahtar dosyası bulunamadı: $GARMIN_KEY_FILE. Garmin kapalı olarak devam ediliyor."
  note "Garmin is off: save your Personal API key (one line, mode 600) as $GARMIN_KEY_FILE, then re-run the installer with --sources garmin (add your other sources) --garmin-key-file $GARMIN_KEY_FILE." \
    "Garmin kapalı: Personal API anahtarınızı (tek satır, izin 600) $GARMIN_KEY_FILE olarak kaydedin, sonra kurulumu --sources garmin (diğer kaynaklarınızla) --garmin-key-file $GARMIN_KEY_FILE ile yeniden çalıştırın."
}

garmin_paste_key() {
  local key
  say "  $GARMIN_KEY_FILE does not exist yet." "  $GARMIN_KEY_FILE henüz yok."
  ask_secret key "Paste your Garmin Personal API key (hidden; empty: leave Garmin off for now)" \
    "Garmin Personal API anahtarınızı yapıştırın (gizli; boş: Garmin şimdilik kapalı)"
  [ -n "$key" ] || return 0
  if [ "$DRY_RUN" = 1 ]; then
    say "  [dry-run] would save the key to $GARMIN_KEY_FILE (mode 600)" "  [dry-run] anahtar $GARMIN_KEY_FILE dosyasına kaydedilirdi (izin 600)"
    GARMIN_PASTED=1
    return 0
  fi
  (umask 077 && mkdir -p "$(dirname "$GARMIN_KEY_FILE")" && printf '%s\n' "$key" >"$GARMIN_KEY_FILE") \
    || die 1 "Could not write $GARMIN_KEY_FILE." "$GARMIN_KEY_FILE yazılamadı."
  ok "Saved the Garmin key to $GARMIN_KEY_FILE (mode 600)." "Garmin anahtarı $GARMIN_KEY_FILE dosyasına kaydedildi (izin 600)."
}

choose_host_jobs() {
  local def_backups=no
  [ "$MODE" = full ] && def_backups=yes
  if [ -z "$BACKUPS" ]; then
    if [ "$INTERACTIVE" = 1 ]; then
      ask_yes_no BACKUPS "$def_backups" "Schedule a daily encrypted backup (03:30)?" "Günlük şifreli yedek planlansın mı (03:30)?"
    else
      BACKUPS="$def_backups"
    fi
  fi
  if [ -z "$AUTOSTART" ]; then
    if [ "$INTERACTIVE" = 1 ]; then
      ask_yes_no AUTOSTART yes "Start HealtOS automatically after a reboot?" "Yeniden başlatmadan sonra HealtOS kendiliğinden açılsın mı?"
    else
      AUTOSTART=yes
    fi
  fi
  if [ -z "$UMAY" ]; then
    if [ "$INTERACTIVE" = 1 ]; then
      say "" ""
      say "Umay (optional) is a chat over your data through your own ChatGPT login (Codex CLI). Your questions" \
        "Umay (isteğe bağlı) kendi ChatGPT girişiniz (Codex CLI) üzerinden verilerinizle sohbettir. Sorularınız"
      say "and the records Umay requests are sent to OpenAI." "ve Umay'ın istediği kayıtlar OpenAI'a gönderilir."
      ask_yes_no UMAY no "Install the Umay bridge service?" "Umay köprü servisi kurulsun mu?"
    else
      UMAY=no
    fi
  fi
}

confirm_plan() {
  step "Plan" "Plan"
  printf '  %s %s\n' "$(t 'Platform:' 'Platform:')" "$PLATFORM"
  printf '  %s %s\n' "$(t 'Mode:' 'Kip:')" "$MODE"
  printf '  %s %s\n' "$(t 'Data sources:' 'Veri kaynakları:')" "$SOURCES"
  [ "$APPLE_RECEIVER" != yes ] || printf '  %s %s\n' "$(t 'Apple receiver:' 'Apple alıcısı:')" yes
  printf '  %s %s\n' "$(t 'Directory:' 'Dizin:')" "$DIR"
  [ "$NEED_CLONE" = 0 ] || printf '  %s %s (%s)\n' "$(t 'Clone:' 'Klonla:')" "$REPO_URL" "$REF"
  printf '  %s %s\n' "$(t 'Daily backup:' 'Günlük yedek:')" "$BACKUPS"
  printf '  %s %s\n' "$(t 'Autostart:' 'Kendiliğinden açılma:')" "$AUTOSTART"
  printf '  %s %s\n' "Umay:" "$UMAY"
  printf '  %s %s\n' "COMPOSE_FILE:" "$(compose_files)"
  [ "$DRY_RUN" = 0 ] || say "  Dry run: nothing is installed, started or cloned." "  Deneme: hiçbir şey kurulmaz, başlatılmaz ya da klonlanmaz."
  [ "$NO_START" = 0 ] || say "  --no-start: containers are not started." "  --no-start: konteynerler başlatılmaz."
  if [ "$INTERACTIVE" = 1 ]; then
    local go
    ask_yes_no go yes "Continue?" "Devam edilsin mi?"
    [ "$go" = yes ] || {
      say "Cancelled; nothing was changed." "İptal edildi; hiçbir şey değiştirilmedi."
      exit 0
    }
  fi
}

# ---- clone ---------------------------------------------------------------------------
clone_repo() {
  [ "$NEED_CLONE" = 1 ] || return 0
  step "Download" "İndirme"
  if [ "$DRY_RUN" = 1 ]; then
    run git clone "$REPO_URL" "$DIR"
    [ "$REF" = main ] || run git -C "$DIR" checkout "$REF"
    return 0
  fi
  have git || die 2 "git is required to download HealtOS. Install: $(pkg_hint git)" \
    "HealtOS'u indirmek için git gerekir. Kurulum: $(pkg_hint git)"
  mkdir -p "$(dirname "$DIR")" || die 1 "Cannot create $(dirname "$DIR")." "$(dirname "$DIR") oluşturulamadı."
  run git clone "$REPO_URL" "$DIR" </dev/null || die 1 "git clone failed (network?)." "git clone başarısız (ağ?)."
  if [ "$REF" != main ]; then
    run git -C "$DIR" checkout --quiet "$REF" </dev/null || die 1 "Ref '$REF' not found." "'$REF' sürümü bulunamadı."
  fi
  is_checkout "$DIR" || die 1 "$DIR does not look like a HealtOS checkout after cloning." "$DIR klonlamadan sonra bir HealtOS kopyasına benzemiyor."
}

# ---- secret files for the app container ----------------------------------------------
share_with_app() {
  # Make a host secret file readable by the app container (uid 10001) through its group:
  # as root it is given to group 10001 with mode 640; otherwise the file's own group gets
  # read access (a shared group never does: secret_blocked turned that source off).
  local f="$1"
  if [ "$DRY_RUN" = 1 ]; then
    if [ "$IS_ROOT" = 1 ]; then
      say "  [dry-run] would give $f to group $APP_UID with mode 640 for the app container" \
        "  [dry-run] $f uygulama konteyneri için $APP_UID grubuna verilip izni 640 yapılırdı"
    else
      say "  [dry-run] would make $f group-readable (mode 640) for the app container" \
        "  [dry-run] $f uygulama konteyneri için grupça okunur yapılırdı (izin 640)"
    fi
    return 0
  fi
  [ "$HOST_OS" = Linux ] || return 0
  if [ "$IS_ROOT" = 1 ]; then
    run chgrp "$APP_UID" "$f" || true
    run chmod 640 "$f" || true
  elif [ -n "$(find "$f" -prune ! -perm -040 2>/dev/null)" ]; then
    run chmod g+r "$f" || true
  fi
}

apple_token_setup() {
  # A receiver left off by secret_blocked still gets its private token file (mode 600), so
  # the printed chgrp/chmod command works; it is not shared and .env is not changed.
  local f
  if [ "$APPLE_BLOCKED" = 1 ]; then
    f="$APPLE_TOKEN_FILE"
  else
    [ "$APPLE_RECEIVER" = yes ] || return 0
    f="$(env_get "$ENV_RESULT" APPLE_RECEIVER_TOKEN_HOST_FILE)"
  fi
  [ -n "$f" ] || return 0
  if [ -f "$f" ]; then
    ok "Apple receiver token file exists; kept (never rotated): $f" "Apple alıcı belirteç dosyası var; korundu (yenilenmedi): $f"
    [ "$APPLE_BLOCKED" = 1 ] || share_with_app "$f"
    return 0
  fi
  if [ "$DRY_RUN" = 1 ]; then
    say "  [dry-run] would create the receiver token $f (43 characters, like 'health-mirror apple-token')" \
      "  [dry-run] alıcı belirteci $f oluşturulurdu (43 karakter, 'health-mirror apple-token' gibi)"
    return 0
  fi
  APPLE_TOKEN_NEW="$(gen_secret token)"
  (umask 077 && mkdir -p "$(dirname "$f")" && printf '%s\n' "$APPLE_TOKEN_NEW" >"$f") \
    || die 1 "Could not write $f." "$f yazılamadı."
  ok "Created the Apple receiver token file $f." "Apple alıcı belirteç dosyası $f oluşturuldu."
  [ "$APPLE_BLOCKED" = 1 ] || share_with_app "$f"
}

garmin_share() {
  local f
  has_source garmin || return 0
  f="$(env_get "$ENV_RESULT" GARMIN_API_KEY_HOST_FILE)"
  [ -n "$f" ] && [ -f "$f" ] || return 0
  share_with_app "$f"
}

# ---- start ---------------------------------------------------------------------------
compose() {
  # Compose reads COMPOSE_FILE from .env; a value in the caller's shell must not override it.
  (cd "$DIR" && unset COMPOSE_FILE COMPOSE_PATH_SEPARATOR && docker compose "$@" </dev/null)
}

check_ready_to_start() {
  local files value
  files="$(env_get "$ENV_RESULT" COMPOSE_FILE)"
  case ":$files:" in
    *:docker-compose.dev.yml:*) ;;
    *)
      # Production guard: the app refuses to start without these.
      for value in APP_UI_TOKEN GOOGLE_CLIENT_ID GOOGLE_CLIENT_SECRET GOOGLE_TOKEN_ENCRYPTION_KEY GOOGLE_HEALTH_WEBHOOK_AUTHORIZATION; do
        [ -n "$(env_get "$ENV_RESULT" "$value")" ] || missing "$value is empty" "$value boş"
      done
      case "$(env_get "$ENV_RESULT" GOOGLE_HEALTH_WEBHOOK_ENDPOINT_URI)" in
        https://*) ;;
        *) missing "GOOGLE_HEALTH_WEBHOOK_ENDPOINT_URI must be https://<your webhook host>/webhooks/google-health" \
          "GOOGLE_HEALTH_WEBHOOK_ENDPOINT_URI https://<webhook-adresiniz>/webhooks/google-health olmalı" ;;
      esac
      ;;
  esac
  case ":$files:" in
    *:docker-compose.garmin.yml:*)
      value="$(env_get "$ENV_RESULT" GARMIN_API_KEY_HOST_FILE)"
      [ -f "$value" ] || missing "Garmin key file missing: ${value:-GARMIN_API_KEY_HOST_FILE is empty}" \
        "Garmin anahtar dosyası yok: ${value:-GARMIN_API_KEY_HOST_FILE boş}"
      ;;
  esac
}

is_live() {
  if have curl; then
    curl -fsS -o /dev/null --max-time 5 "$1" 2>/dev/null
  elif have wget; then
    wget -q -O /dev/null -T 5 "$1" 2>/dev/null
  else
    return 1
  fi
}

start_stack() {
  local port url i limit=300 status_dir
  check_ready_to_start
  [ "$PLATFORM" != raspberrypi ] || limit=900
  if [ -n "$MISSING" ]; then
    START_SKIPPED=missing
    return 0
  fi
  if [ "$NO_START" = 1 ]; then
    START_SKIPPED=no-start
    return 0
  fi
  step "Start" "Başlatma"
  status_dir="$(env_get "$ENV_RESULT" BACKUP_STATUS_HOST_DIR)"
  status_dir="${status_dir:-./backup-status}"
  case "$status_dir" in /*) ;; *) status_dir="$DIR/${status_dir#./}" ;; esac
  if [ ! -d "$status_dir" ]; then
    # The host owns backup metadata; created before Compose would create it as root.
    run mkdir -p "$status_dir" && run chmod 755 "$status_dir"
  fi
  if [ "$DRY_RUN" = 1 ]; then
    run docker compose up -d --build
    say "  [dry-run] would wait for http://127.0.0.1:<APP_PORT>/health/live" "  [dry-run] http://127.0.0.1:<APP_PORT>/health/live beklenirdi"
    return 0
  fi
  [ "$PLATFORM" != raspberrypi ] || say "  The first build on a Raspberry Pi can take 15-30 minutes." "  Raspberry Pi'de ilk derleme 15-30 dakika sürebilir."
  port="$(env_get "$ENV_RESULT" APP_PORT)"
  url="http://127.0.0.1:${port:-8000}/health/live"
  if ! is_live "$url"; then
    if port_busy "${port:-8000}"; then
      warn "Port ${port:-8000} is already used by another program; set APP_PORT in .env if the start fails." \
        "${port:-8000} portu başka bir program tarafından kullanılıyor; başlatma başarısız olursa .env içinde APP_PORT ayarlayın."
    fi
    case ":$(env_get "$ENV_RESULT" COMPOSE_FILE):" in
      *:docker-compose.dev.yml:*)
        if port_busy 5432; then
          warn "Port 5432 is in use (another PostgreSQL?); trial mode publishes the database on 127.0.0.1:5432." \
            "5432 portu kullanımda (başka bir PostgreSQL?); deneme kipi veritabanını 127.0.0.1:5432'de açar."
        fi
        ;;
    esac
  fi
  printf '  $ docker compose up -d --build\n'
  compose up -d --build || die 1 "docker compose up failed. See the output above; check: docker compose logs --tail=80 app" \
    "docker compose up başarısız. Yukarıdaki çıktıya bakın; kontrol: docker compose logs --tail=80 app"
  if ! have curl && ! have wget; then
    warn "No curl/wget: skipped the health check. Check: docker compose ps" "curl/wget yok: sağlık kontrolü atlandı. Kontrol: docker compose ps"
    STARTED=1
    return 0
  fi
  printf '  %s ' "$(t "Waiting for $url" "$url bekleniyor")"
  i=0
  while [ "$i" -lt "$limit" ]; do
    if is_live "$url"; then
      printf '\n'
      ok "HealtOS is up." "HealtOS çalışıyor."
      STARTED=1
      return 0
    fi
    printf '.'
    sleep 5
    i=$((i + 5))
  done
  printf '\n'
  die 1 "HealtOS did not answer /health/live within ${limit}s. Check: docker compose ps; docker compose logs --tail=80 app" \
    "HealtOS ${limit} sn içinde /health/live'a yanıt vermedi. Kontrol: docker compose ps; docker compose logs --tail=80 app"
}

# ---- host jobs: systemd --------------------------------------------------------------
has_systemd() {
  if [ "$HOST_OS" != Linux ]; then
    # Dry-run preview for another host: assume a systemd Linux target.
    [ "$DRY_RUN" = 1 ]
    return
  fi
  [ -d /run/systemd/system ] && have systemctl
}

render_unit() {
  # render_unit SRC system|user: /opt/healthos -> checkout; user units drop root-only lines.
  local src="$1" kind="$2" cfg="$HOME/.config/healtos" path_line="/usr/local/bin:/usr/bin:/bin" codex_dir=""
  if have codex; then codex_dir="$(dirname "$(command -v codex)")"; fi
  # The system bridge runs with ProtectHome=true: a CLI under /home or /root is unreachable.
  if [ "$kind" = system ]; then case "$codex_dir" in /home/* | /root/*) codex_dir="" ;; esac; fi
  case ":$path_line:" in *":$codex_dir:"*) ;; *) [ -z "$codex_dir" ] || path_line="$codex_dir:$path_line" ;; esac
  if [ "$kind" = system ]; then
    HOS_DIR="$DIR" HOS_PATH="$path_line" awk '
      { gsub("/opt/healthos", ENVIRON["HOS_DIR"]) }
      /^Environment=PATH=/ { $0 = "Environment=PATH=" ENVIRON["HOS_PATH"] }
      { print }' "$src"
  else
    HOS_DIR="$DIR" HOS_CFG="$cfg" HOS_PATH="$HOME/.local/bin:$path_line" awk '
      /^(User|Group|Requires|PrivateTmp|ProtectSystem|ProtectHome|ReadWritePaths|StateDirectory|StateDirectoryMode|Wants)=/ { next }
      /^Environment=(HOME|CODEX_HOME)=/ { next }
      /^After=/ {
        sub(/ ?docker\.service/, ""); sub(/ ?network-online\.target/, "")
        if ($0 == "After=") next
      }
      /^EnvironmentFile=/ {
        file = $0; sub(/^EnvironmentFile=-?/, "", file); sub("^/etc/healthos", ENVIRON["HOS_CFG"], file)
        if (file ~ /backup\.env$/) print "Environment=\"BACKUP_KEY_FILE=" ENVIRON["HOS_CFG"] "/backup.key\""
        $0 = "EnvironmentFile=-" file
      }
      /^Environment=PATH=/ { $0 = "Environment=PATH=" ENVIRON["HOS_PATH"] }
      /^WantedBy=multi-user\.target/ { $0 = "WantedBy=default.target" }
      { gsub("/opt/healthos", ENVIRON["HOS_DIR"]); print }' "$src"
  fi
}

install_file() {
  # install_file CONTENT_FILE DEST MODE: keeps an existing different file as DEST.bak-<stamp>.
  local content="$1" dest="$2" mode="$3" bak
  if [ -f "$dest" ] && cmp -s "$content" "$dest"; then
    ok "$dest is up to date." "$dest güncel."
    return 0
  fi
  if [ -f "$dest" ]; then
    bak="$dest.bak-$(date -u +%Y%m%dT%H%M%SZ)"
    run cp -p "$dest" "$bak" || return 1
  fi
  [ -d "$(dirname "$dest")" ] || run mkdir -p "$(dirname "$dest")" || return 1
  if [ "$DRY_RUN" = 1 ]; then
    printf '  [dry-run] write %s (mode %s)\n' "$dest" "$mode"
    return 0
  fi
  cp "$content" "$dest" && chmod "$mode" "$dest" && printf '  %s %s\n' "$(t 'wrote' 'yazıldı')" "$dest"
}

RENDER_TMP=""
rendered() {
  # rendered SRC KIND -> path of a private temporary rendering (reused buffer file).
  if [ -z "$RENDER_TMP" ]; then
    RENDER_TMP="$(mktemp "${TMPDIR:-/tmp}/healtos-unit.XXXXXXXX")" || die 1 "mktemp failed" "mktemp başarısız"
  fi
  render_unit "$1" "$2" >"$RENDER_TMP"
  printf '%s' "$RENDER_TMP"
}

choose_unit_mode() {
  local idx
  if [ "$IS_ROOT" = 1 ]; then
    UNIT_MODE=system
    return 0
  fi
  if [ "$INTERACTIVE" = 1 ]; then
    menu idx 2 "System services need root. How should the host jobs be installed?" "Sistem servisleri root ister. Bilgisayar görevleri nasıl kurulsun?" \
      "Stop here; I will re-run with sudo (system units in /etc/systemd/system)" \
      "Burada dur; sudo ile yeniden çalıştıracağım (/etc/systemd/system içinde sistem birimleri)" \
      "Rootless: user units (systemctl --user) with lingering, under ~/.config/systemd/user" \
      "Rootsuz: kullanıcı birimleri (systemctl --user), lingering açık, ~/.config/systemd/user altında"
    if [ "$idx" = 1 ]; then
      say "Re-run: sudo bash install.sh $(t '(your .env is kept as is)' '(.env olduğu gibi korunur)')" \
        "Yeniden çalıştırın: sudo bash install.sh (.env olduğu gibi korunur)"
      UNIT_MODE=none
      return 0
    fi
  else
    say "  Not root: installing user units (systemctl --user, lingering on). For system units run with sudo." \
      "  Root değil: kullanıcı birimleri kuruluyor (systemctl --user, lingering açık). Sistem birimleri için sudo ile çalıştırın."
  fi
  UNIT_MODE=user
}

ensure_age() {
  if have age && have age-keygen; then return 0; fi
  warn "age is not installed; backups need it. Install: $(pkg_hint age)" "age kurulu değil; yedekler için gerekir. Kurulum: $(pkg_hint age)"
  return 1
}

make_backup_key() {
  # make_backup_key PATH: create an age identity outside the repo and .env, never overwrite.
  local key="$1" create=yes
  BACKUP_KEY_PATH="$key"
  if [ -f "$key" ]; then
    ok "Backup key exists; kept: $key" "Yedek anahtarı var; korundu: $key"
    if have age-keygen && [ -r "$key" ] && grep -q '^AGE-SECRET-KEY-' "$key" 2>/dev/null; then
      BACKUP_PUBKEY="$(age-keygen -y "$key" 2>/dev/null || true)"
    fi
    return 0
  fi
  if [ "$INTERACTIVE" = 1 ]; then
    ask_yes_no create yes "Create an age backup key at $key?" "$key konumunda bir age yedek anahtarı oluşturulsun mu?"
  fi
  if [ "$create" != yes ]; then
    note "No backup key was created: put an age identity at $key (mode 600) or set BACKUP_AGE_RECIPIENT before 03:30." \
      "Yedek anahtarı oluşturulmadı: 03:30'dan önce $key konumuna bir age kimliği koyun (izin 600) ya da BACKUP_AGE_RECIPIENT ayarlayın."
    return 0
  fi
  if [ "$DRY_RUN" = 1 ]; then
    [ -d "$(dirname "$key")" ] || printf '  [dry-run] (umask 077; mkdir -p %s)\n' "$(dirname "$key")"
    printf '  [dry-run] (umask 077; age-keygen -o %s)\n' "$key"
    return 0
  fi
  ensure_age || return 1
  (umask 077 && mkdir -p "$(dirname "$key")" && age-keygen -o "$key" 2>/dev/null) \
    || die 1 "Could not create the backup key $key." "Yedek anahtarı $key oluşturulamadı."
  chmod 600 "$key"
  BACKUP_PUBKEY="$(age-keygen -y "$key" 2>/dev/null || true)"
  ok "Created the backup key $key (mode 600)." "Yedek anahtarı $key oluşturuldu (izin 600)."
}

python_hint() {
  python_ok && return 0
  warn "Backups need Python 3.11+ on the host (stdlib only) for the status file. Install: $(pkg_hint python3)" \
    "Yedekler durum dosyası için bilgisayarda Python 3.11+ ister (yalnız standart kütüphane). Kurulum: $(pkg_hint python3)"
}

systemctl_as() {
  if [ "$UNIT_MODE" = user ]; then run systemctl --user "$@"; else run systemctl "$@"; fi
}

linux_backups() {
  local unitdir key name
  [ "$BACKUPS" = yes ] || return 0
  step "Daily encrypted backup" "Günlük şifreli yedek"
  python_hint
  if [ "$UNIT_MODE" = user ]; then
    unitdir="$HOME/.config/systemd/user"
    key="$HOME/.config/healtos/backup.key"
  else
    unitdir="/etc/systemd/system"
    key="/etc/healthos/backup.key"
  fi
  if [ "$DRY_RUN" = 0 ] && ! ensure_age; then
    note "Backups were not scheduled: install age, then re-run the installer." \
      "Yedekler planlanmadı: age kurun, sonra kurulumu yeniden çalıştırın."
    return 0
  fi
  make_backup_key "$key" || return 0
  for name in healthos-backup.service healthos-backup.timer healthos-backup-failure.service; do
    install_file "$(rendered "$DIR/deploy/systemd/$name" "$UNIT_MODE")" "$unitdir/$name" 644 \
      || die 1 "Could not install $unitdir/$name." "$unitdir/$name kurulamadı."
  done
  if [ "$UNIT_MODE" = user ]; then enable_linger; fi
  systemctl_as daemon-reload || warn "systemctl daemon-reload failed." "systemctl daemon-reload başarısız."
  systemctl_as enable --now healthos-backup.timer \
    || warn "Could not enable healthos-backup.timer; run it by hand (see above)." "healthos-backup.timer etkinleştirilemedi; elle çalıştırın (yukarıya bakın)."
  if [ "$UNIT_MODE" = user ]; then
    say "  Run once now: systemctl --user start healthos-backup.service; journalctl --user -u healthos-backup -n 20" \
      "  Şimdi bir kez: systemctl --user start healthos-backup.service; journalctl --user -u healthos-backup -n 20"
  else
    say "  Run once now: sudo systemctl start healthos-backup.service; journalctl -u healthos-backup -n 20" \
      "  Şimdi bir kez: sudo systemctl start healthos-backup.service; journalctl -u healthos-backup -n 20"
  fi
}

enable_linger() {
  [ "$UNIT_MODE" = user ] || return 0
  if have loginctl; then
    run loginctl enable-linger "$(id -un)" \
      || note "Lingering could not be enabled: run 'sudo loginctl enable-linger $(id -un)' so user units run without a login." \
        "Lingering açılamadı: kullanıcı birimleri oturum olmadan çalışsın diye 'sudo loginctl enable-linger $(id -un)' çalıştırın."
  fi
}

linux_autostart() {
  [ "$AUTOSTART" = yes ] || return 0
  step "Autostart after reboot" "Yeniden başlatmada açılma"
  say "  Containers use 'restart: unless-stopped': they come back whenever Docker starts." \
    "  Konteynerler 'restart: unless-stopped' kullanır: Docker her başladığında geri gelir."
  if [ "$IS_ROOT" = 1 ]; then
    run systemctl enable docker.service || warn "Could not enable docker.service." "docker.service etkinleştirilemedi."
    have_unit containerd.service && { run systemctl enable containerd.service || true; }
  elif [ "$DRY_RUN" = 0 ] && [ "$(systemctl is-enabled docker.service 2>/dev/null)" = enabled ]; then
    ok "docker.service starts at boot." "docker.service açılışta başlıyor."
  elif docker info --format '{{.SecurityOptions}}' 2>/dev/null </dev/null | grep -q rootless; then
    run systemctl --user enable docker.service || true
    enable_linger
  else
    note "Make Docker start at boot: sudo systemctl enable docker" "Docker'ın açılışta başlaması için: sudo systemctl enable docker"
  fi
}

have_unit() { systemctl list-unit-files "$1" 2>/dev/null | grep -q "^$1"; }

uv_sync() {
  if ! have uv; then
    note "Umay needs uv: curl -LsSf https://astral.sh/uv/install.sh | sh, then re-run with --umay yes." \
      "Umay uv ister: curl -LsSf https://astral.sh/uv/install.sh | sh, sonra --umay yes ile yeniden çalıştırın."
    return 1
  fi
  if [ "$UNIT_MODE" = system ]; then
    # Keep uv's Python inside the checkout: the service account cannot read /root.
    (cd "$DIR" && export UV_PYTHON_INSTALL_DIR="$DIR/.uv-tool/python" && run uv sync --frozen --no-dev --python "$BRIDGE_PYTHON" </dev/null)
  else
    (cd "$DIR" && run uv sync --frozen --no-dev --python "$BRIDGE_PYTHON" </dev/null)
  fi
}

codex_hint() {
  if ! have codex; then
    note "Umay needs the Codex CLI on PATH (https://developers.openai.com/codex/cli) and 'codex login'." \
      "Umay için Codex CLI PATH üzerinde olmalı (https://developers.openai.com/codex/cli) ve 'codex login' yapılmalı."
  fi
}

linux_umay() {
  local unit dest token envfile
  [ "$UMAY" = yes ] || return 0
  step "Umay bridge" "Umay köprüsü"
  say "  Privacy: your questions and the records Umay requests are sent to OpenAI." \
    "  Gizlilik: sorularınız ve Umay'ın istediği kayıtlar OpenAI'a gönderilir."
  codex_hint
  uv_sync || return 0
  if [ "$UNIT_MODE" = system ]; then
    case "$DIR" in /home/* | /root/*)
      note "The bridge unit uses ProtectHome=true: move the checkout outside /home (e.g. /opt/healthos) for the service." \
        "Köprü birimi ProtectHome=true kullanır: servis için kopyayı /home dışına taşıyın (örn. /opt/healthos)." ;;
    esac
    if ! id healthos-bridge >/dev/null 2>&1; then
      run useradd --system --user-group --home-dir /var/lib/healthos-bridge \
        --shell "$(command -v nologin || echo /usr/sbin/nologin)" healthos-bridge \
        || die 1 "Could not create the healthos-bridge account." "healthos-bridge hesabı oluşturulamadı."
    fi
    run install -d -m 700 -o healthos-bridge -g healthos-bridge /var/lib/healthos-bridge
    # An existing /etc/healthos keeps its mode; it is created private only when missing.
    [ -d /etc/healthos ] || run install -d -m 700 /etc/healthos
    envfile=/etc/healthos/bridge.env
    if [ -f "$envfile" ]; then
      ok "$envfile exists; kept." "$envfile var; korundu."
    elif [ "$DRY_RUN" = 1 ]; then
      say "  [dry-run] would write $envfile (mode 600) with CODEX_BRIDGE_TOKEN from .env" \
        "  [dry-run] .env'deki CODEX_BRIDGE_TOKEN ile $envfile yazılırdı (izin 600)"
    else
      token="$(env_get "$ENV_RESULT" CODEX_BRIDGE_TOKEN)"
      (umask 077 && printf 'CODEX_BRIDGE_TOKEN=%s\n' "$token" >"$envfile") || die 1 "Could not write $envfile." "$envfile yazılamadı."
      ok "Wrote $envfile (mode 600)." "$envfile yazıldı (izin 600)."
    fi
    dest=/etc/systemd/system/healthos-bridge.service
  else
    dest="$HOME/.config/systemd/user/healthos-bridge.service"
    enable_linger
  fi
  unit="$(rendered "$DIR/deploy/systemd/healthos-bridge.service" "$UNIT_MODE")"
  install_file "$unit" "$dest" 644 || die 1 "Could not install $dest." "$dest kurulamadı."
  systemctl_as daemon-reload || true
  if [ "$UNIT_MODE" = system ] && [ ! -f /var/lib/healthos-bridge/auth.json ] && [ ! -f /var/lib/healthos-bridge/healthos-bridge/auth.json ]; then
    systemctl_as enable healthos-bridge.service || true
    note "Sign the bridge account in, then start it: sudo -u healthos-bridge env HOME=/var/lib/healthos-bridge CODEX_HOME=/var/lib/healthos-bridge codex login && sudo systemctl start healthos-bridge" \
      "Köprü hesabıyla giriş yapıp başlatın: sudo -u healthos-bridge env HOME=/var/lib/healthos-bridge CODEX_HOME=/var/lib/healthos-bridge codex login && sudo systemctl start healthos-bridge"
  elif [ "$UNIT_MODE" = user ] && [ ! -f "$HOME/.codex/auth.json" ]; then
    systemctl_as enable healthos-bridge.service || true
    note "Sign in with 'codex login', then: systemctl --user start healthos-bridge" \
      "'codex login' ile giriş yapın, sonra: systemctl --user start healthos-bridge"
  elif [ "$NO_START" = 1 ]; then
    systemctl_as enable healthos-bridge.service || true
  else
    systemctl_as enable --now healthos-bridge.service || warn "Could not start healthos-bridge." "healthos-bridge başlatılamadı."
  fi
  say "  Restrict port 8766 to loopback and Docker bridges: deploy/firewall/ (nftables or ufw)." \
    "  8766 portunu loopback ve Docker köprüleriyle sınırlayın: deploy/firewall/ (nftables ya da ufw)."
}

linux_no_systemd() {
  local key
  step "Host jobs (no systemd found)" "Bilgisayar görevleri (systemd bulunamadı)"
  if [ "$IS_ROOT" = 1 ]; then key=/etc/healthos/backup.key; else key="$HOME/.config/healtos/backup.key"; fi
  if [ "$BACKUPS" = yes ]; then
    if [ "$DRY_RUN" = 1 ] || ensure_age; then make_backup_key "$key" || true; fi
    say "  Add the daily backup to cron (crontab -e):" "  Günlük yedeği cron'a ekleyin (crontab -e):"
    printf '    30 3 * * * cd %s && BACKUP_KEY_FILE=%s /bin/bash scripts/backup.sh >>%s/backups/backup.log 2>&1\n' "$DIR" "$key" "$DIR"
  fi
  if [ "$AUTOSTART" = yes ]; then
    say "  Make Docker start at boot with your init system (OpenRC: rc-update add docker default)." \
      "  Docker'ın açılışta başlamasını init sisteminizle sağlayın (OpenRC: rc-update add docker default)."
  fi
  if [ "$UMAY" = yes ]; then
    say "  Umay without systemd: uv run --frozen --no-dev python scripts/start_codex_bridge.py (after each reboot)." \
      "  systemd olmadan Umay: uv run --frozen --no-dev python scripts/start_codex_bridge.py (her açılıştan sonra)."
  fi
}

linux_jobs() {
  [ "$BACKUPS" = yes ] || [ "$AUTOSTART" = yes ] || [ "$UMAY" = yes ] || return 0
  if ! has_systemd; then
    linux_no_systemd
    return 0
  fi
  if [ "$BACKUPS" = yes ] || [ "$UMAY" = yes ]; then
    case "$DIR" in
      *[!A-Za-z0-9._/@+-]*)
        UNIT_MODE=none
        note "systemd units were not installed: the path $DIR contains spaces or special characters. Use a plain path such as /opt/healthos." \
          "systemd birimleri kurulmadı: $DIR yolu boşluk ya da özel karakter içeriyor. /opt/healthos gibi düz bir yol kullanın."
        ;;
      *) choose_unit_mode ;;
    esac
  else
    UNIT_MODE=none
  fi
  if [ "$UNIT_MODE" != none ]; then
    linux_backups
    linux_umay
  fi
  linux_autostart
}

# ---- host jobs: macOS launchd --------------------------------------------------------
xml_escape() { printf '%s' "$1" | sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g'; }
sed_escape() { printf '%s' "$1" | sed 's/[\\|&]/\\&/g'; }

mac_path() {
  local p="" d tool
  for tool in docker age python3 codex uv colima orb; do
    have "$tool" || continue
    d="$(dirname "$(command -v "$tool")")"
    case ":$p:" in *":$d:"*) ;; *) p="$p:$d" ;; esac
  done
  for d in /opt/homebrew/bin /usr/local/bin "$HOME/.orbstack/bin" /usr/bin /bin /usr/sbin /sbin; do
    case ":$p:" in *":$d:"*) ;; *) p="$p:$d" ;; esac
  done
  printf '%s' "${p#:}"
}

render_plist() {
  # render_plist TEMPLATE KEYFILE LOGDIR
  sed -e "s|@HEALTOS_DIR@|$(sed_escape "$(xml_escape "$DIR")")|g" \
    -e "s|@PATH@|$(sed_escape "$(xml_escape "$(mac_path)")")|g" \
    -e "s|@BACKUP_KEY_FILE@|$(sed_escape "$(xml_escape "$2")")|g" \
    -e "s|@LOG_DIR@|$(sed_escape "$(xml_escape "$3")")|g" "$1"
}

load_agent() {
  local label="$1" plist="$2" domain
  domain="gui/$(id -u)"
  if [ "$DRY_RUN" = 0 ] && launchctl print "$domain/$label" >/dev/null 2>&1; then
    run launchctl bootout "$domain/$label" || true
  fi
  run launchctl bootstrap "$domain" "$plist" \
    || warn "launchctl bootstrap failed for $label; try: launchctl bootstrap $domain $plist" \
      "$label için launchctl bootstrap başarısız; deneyin: launchctl bootstrap $domain $plist"
}

mac_jobs() {
  local agents="$HOME/Library/LaunchAgents" support="$HOME/Library/Application Support/HealtOS" logs="$HOME/Library/Logs/HealtOS" tmp
  if [ "$BACKUPS" = yes ]; then
    step "Daily encrypted backup (launchd)" "Günlük şifreli yedek (launchd)"
    python_hint
    if [ "$DRY_RUN" = 0 ] && ! ensure_age; then
      note "Backups were not scheduled: brew install age, then re-run the installer." \
        "Yedekler planlanmadı: brew install age, sonra kurulumu yeniden çalıştırın."
    else
      make_backup_key "$support/backup.key" || true
      run mkdir -p "$logs"
      tmp="$(mktemp "${TMPDIR:-/tmp}/healtos-plist.XXXXXXXX")"
      render_plist "$DIR/deploy/launchd/org.healtos.backup.plist" "$support/backup.key" "$logs" >"$tmp"
      install_file "$tmp" "$agents/org.healtos.backup.plist" 644 || die 1 "Could not install the backup agent." "Yedek ajanı kurulamadı."
      rm -f "$tmp"
      load_agent org.healtos.backup "$agents/org.healtos.backup.plist"
      say "  Run once now: launchctl kickstart gui/$(id -u)/org.healtos.backup; log: $logs/backup.log" \
        "  Şimdi bir kez: launchctl kickstart gui/$(id -u)/org.healtos.backup; günlük: $logs/backup.log"
    fi
  fi
  if [ "$AUTOSTART" = yes ]; then
    step "Autostart after login" "Oturum açılışında başlatma"
    say "  Containers use 'restart: unless-stopped': they come back whenever Docker starts." \
      "  Konteynerler 'restart: unless-stopped' kullanır: Docker her başladığında geri gelir."
    case "$(docker_runtime_mac)" in
      colima)
        if have brew; then
          run brew services start colima || warn "brew services start colima failed." "brew services start colima başarısız."
        else
          note "Start Colima at login: brew services start colima" "Colima'yı oturum açılışında başlatın: brew services start colima"
        fi
        ;;
      orbstack)
        note "OrbStack: Settings > General > 'Start at login' (on by default)." "OrbStack: Settings > General > 'Start at login' (varsayılan açık)."
        ;;
      *)
        if grep -qi '"autostart"[[:space:]]*:[[:space:]]*true' "$HOME/Library/Group Containers/group.com.docker/settings-store.json" \
          "$HOME/Library/Group Containers/group.com.docker/settings.json" 2>/dev/null; then
          ok "Docker Desktop starts when you sign in." "Docker Desktop oturum açılınca başlıyor."
        else
          note "Docker Desktop: Settings > General > 'Start Docker Desktop when you sign in to your computer'." \
            "Docker Desktop: Settings > General > 'Start Docker Desktop when you sign in to your computer' seçeneğini açın."
        fi
        ;;
    esac
  fi
  if [ "$UMAY" = yes ]; then
    step "Umay bridge (launchd)" "Umay köprüsü (launchd)"
    say "  Privacy: your questions and the records Umay requests are sent to OpenAI." \
      "  Gizlilik: sorularınız ve Umay'ın istediği kayıtlar OpenAI'a gönderilir."
    codex_hint
    UNIT_MODE=user
    if uv_sync; then
      run mkdir -p "$logs"
      tmp="$(mktemp "${TMPDIR:-/tmp}/healtos-plist.XXXXXXXX")"
      render_plist "$DIR/deploy/launchd/org.healtos.bridge.plist" "" "$logs" >"$tmp"
      install_file "$tmp" "$agents/org.healtos.bridge.plist" 644 || die 1 "Could not install the bridge agent." "Köprü ajanı kurulamadı."
      rm -f "$tmp"
      if [ -f "$HOME/.codex/auth.json" ] || [ "$DRY_RUN" = 1 ]; then
        [ "$NO_START" = 1 ] || load_agent org.healtos.bridge "$agents/org.healtos.bridge.plist"
      else
        note "Sign in with 'codex login', then: launchctl bootstrap gui/$(id -u) $agents/org.healtos.bridge.plist" \
          "'codex login' ile giriş yapın, sonra: launchctl bootstrap gui/$(id -u) $agents/org.healtos.bridge.plist"
      fi
    fi
  fi
}

# ---- host jobs: NAS ------------------------------------------------------------------
nas_guidance() {
  step "NAS steps" "NAS adımları"
  say "  .env is ready. The installer does not install services on a NAS; use the NAS tools:" \
    "  .env hazır. Kurulum NAS'a servis kurmaz; NAS araçlarını kullanın:"
  say "  - Start: over SSH in $DIR run 'docker compose up -d --build' (reads COMPOSE_FILE from .env)." \
    "  - Başlatma: SSH ile $DIR içinde 'docker compose up -d --build' çalıştırın (COMPOSE_FILE'ı .env'den okur)."
  say "    Synology Container Manager > Project > Create, path $DIR; QNAP Container Station > Applications;" \
    "    Synology Container Manager > Proje > Oluştur, yol $DIR; QNAP Container Station > Applications;"
  say "    Unraid: Compose Manager plugin; TrueNAS SCALE: run it from the shell. GUIs may ignore COMPOSE_FILE:" \
    "    Unraid: Compose Manager eklentisi; TrueNAS SCALE: kabuktan çalıştırın. Arayüzler COMPOSE_FILE'ı yok sayabilir:"
  say "    then list the same files there (see COMPOSE_FILE in .env)." "    o zaman aynı dosyaları orada seçin (.env içindeki COMPOSE_FILE)."
  say "  - Autostart: containers use 'restart: unless-stopped'; enable the Docker/Container package at boot." \
    "  - Açılışta başlatma: konteynerler 'restart: unless-stopped' kullanır; Docker/Container paketini açılışta etkinleştirin."
  if [ "$BACKUPS" = yes ]; then
    say "  - Daily backup: add a scheduled task (Synology Task Scheduler, QNAP crontab, Unraid User Scripts," \
      "  - Günlük yedek: zamanlanmış görev ekleyin (Synology Görev Zamanlayıcı, QNAP crontab, Unraid User Scripts,"
    say "    TrueNAS Cron Jobs) that runs as root at 03:30, with age installed (static binary if no package):" \
      "    TrueNAS Cron Jobs); root olarak 03:30'da çalışsın, age kurulu olsun (paket yoksa statik dosya):"
    printf '      cd %s && BACKUP_KEY_FILE=/root/.config/healtos/backup.key /bin/bash scripts/backup.sh\n' "$DIR"
    say "    Create the key once: age-keygen -o /root/.config/healtos/backup.key (copy it off the NAS!)." \
      "    Anahtarı bir kez oluşturun: age-keygen -o /root/.config/healtos/backup.key (NAS dışına kopyalayın!)."
  fi
  [ "$UMAY" != yes ] || say "  - Umay on a NAS: run the bridge by hand (docs/DEPLOYMENT.md, step 9)." \
    "  - NAS'ta Umay: köprüyü elle çalıştırın (docs/DEPLOYMENT.md, 9. adım)."
  say "  Full guide: docs/INSTALL.md (NAS section)." "  Tam rehber: docs/INSTALL.md (NAS bölümü)."
}

host_jobs() {
  case "$PLATFORM" in
    nas) nas_guidance ;;
    macos) mac_jobs ;;
    *) linux_jobs ;;
  esac
}

# ---- summary -------------------------------------------------------------------------
summary() {
  local port token url redirect key_note endpoint
  port="$(env_get "$ENV_RESULT" APP_PORT)"
  port="${port:-8000}"
  url="http://localhost:$port"
  token="$(env_get "$ENV_RESULT" APP_UI_TOKEN)"
  step "Summary" "Özet"
  if [ "$DRY_RUN" = 1 ]; then
    say "  Dry run finished: nothing was installed or started. Preview: $ENV_RESULT" \
      "  Deneme bitti: hiçbir şey kurulmadı ya da başlatılmadı. Önizleme: $ENV_RESULT"
  elif [ "$STARTED" = 1 ]; then
    ok "HealtOS is running." "HealtOS çalışıyor."
  elif [ "$START_SKIPPED" = missing ]; then
    warn "Configured, NOT started: the values below are still missing." "Yapılandırıldı, BAŞLATILMADI: aşağıdaki değerler hâlâ eksik."
  else
    ok "Configured (not started: --no-start)." "Yapılandırıldı (başlatılmadı: --no-start)."
  fi
  if [ -n "$MISSING" ]; then
    say "  Still missing in .env ($MODE mode):" "  .env içinde hâlâ eksik ($MODE kipi):"
    printf '%s' "$MISSING" | sed 's/^/    - /'
    say "    Google OAuth client: README step 3 / docs/INSTALL.md; webhook: docs/CLOUDFLARE_TUNNEL.md." \
      "    Google OAuth istemcisi: README 3. adım / docs/INSTALL.md; webhook: docs/CLOUDFLARE_TUNNEL.md."
    say "    Then: cd $DIR && docker compose up -d --build   (or re-run this installer)" \
      "    Sonra: cd $DIR && docker compose up -d --build   (ya da bu kurulumu yeniden çalıştırın)"
  fi
  printf '\n  URL: %s  (%s http://healthos.localhost:%s)\n' "$url" "$(t 'or' 'ya da')" "$port"
  printf '  %s %s\n' "$(t 'UI token:' 'Arayüz anahtarı:')" "$token"
  say "  Keep the token private (password manager); it is the dashboard password." \
    "  Anahtarı gizli tutun (parola yöneticisi); panelin parolasıdır."
  case "$PLATFORM" in
    linux | vps | raspberrypi | nas)
      say "  On a remote server, open an SSH tunnel from your own computer, then the URL above:" \
        "  Uzak sunucuda kendi bilgisayarınızdan SSH tüneli açın, sonra yukarıdaki adresi açın:"
      printf '    ssh -N -L %s:127.0.0.1:%s %s@%s\n' "$port" "$port" "$(id -un)" "$(hostname 2>/dev/null || echo server)"
      ;;
  esac
  say "" ""
  say "  Next steps:" "  Sonraki adımlar:"
  say "  - Open the URL and sign in with the UI token. Manage: cd $DIR && docker compose ps | logs -f app" \
    "  - Adresi açıp arayüz anahtarıyla giriş yapın. Yönetim: cd $DIR && docker compose ps | logs -f app"
  say "  - Update later: git pull && docker compose up -d --build. Add a source: re-run with --sources ..." \
    "  - Güncelleme: git pull && docker compose up -d --build. Kaynak eklemek: --sources ... ile yeniden çalıştırın"
  if has_source google; then
    redirect="$(env_get "$ENV_RESULT" GOOGLE_REDIRECT_URI)"
    say "  - Google: redirect URI in your OAuth client: ${redirect:-http://localhost:8000/auth/google/callback}" \
      "  - Google: OAuth istemcinizdeki yönlendirme URI'si: ${redirect:-http://localhost:8000/auth/google/callback}"
    say "    Connect: $url/auth/google/start, then: docker compose exec app health-mirror backfill" \
      "    Bağlan: $url/auth/google/start, sonra: docker compose exec app health-mirror backfill"
    say "    GOOGLE_TOKEN_ENCRYPTION_KEY in .env: store it in a password manager too (a restore needs it)." \
      "    .env içindeki GOOGLE_TOKEN_ENCRYPTION_KEY: parola yöneticisinde de saklayın (geri yükleme ister)."
  fi
  if has_source garmin; then
    say "  - Garmin: status: docker compose exec app health-mirror garmin-status; history: health-mirror garmin-backfill" \
      "  - Garmin: durum: docker compose exec app health-mirror garmin-status; geçmiş: health-mirror garmin-backfill"
  fi
  if has_source apple; then
    say "  - Apple Health: iPhone Health app > profile > Export All Health Data; upload export.zip on the System page" \
      "  - Apple Sağlık: iPhone Sağlık > profil > Tüm Sağlık Verilerini Dışa Aktar; export.zip'i Sistem sayfasından yükleyin"
    say "    (Sistem > Apple Sağlık içe aktar). Status: docker compose exec app health-mirror apple-status" \
      "    (Sistem > Apple Sağlık içe aktar). Durum: docker compose exec app health-mirror apple-status"
  fi
  if [ "$APPLE_RECEIVER" = yes ]; then
    endpoint="$(env_get "$ENV_RESULT" APPLE_RECEIVER_ENDPOINT_URI)"
    if [ -n "$endpoint" ]; then
      say "    Receiver: POST $endpoint" "    Alıcı: POST $endpoint"
    else
      say "    Receiver: set APPLE_RECEIVER_ENDPOINT_URI=https://<your host>/webhooks/apple-health in .env" \
        "    Alıcı: .env içinde APPLE_RECEIVER_ENDPOINT_URI=https://<adresiniz>/webhooks/apple-health ayarlayın"
    fi
    if [ -n "$APPLE_TOKEN_NEW" ]; then
      say "    Add this header to the Health Auto Export REST API automation (shown once):" \
        "    Bu başlığı Health Auto Export REST API otomasyonuna ekleyin (bir kez gösterilir):"
      printf '      Authorization: Bearer %s\n' "$APPLE_TOKEN_NEW"
    else
      say "    Header: Authorization: Bearer <content of APPLE_RECEIVER_TOKEN_HOST_FILE>" \
        "    Başlık: Authorization: Bearer <APPLE_RECEIVER_TOKEN_HOST_FILE içeriği>"
    fi
  fi
  if [ "$BACKUPS" = yes ] && [ -n "$BACKUP_KEY_PATH" ]; then
    key_note="$BACKUP_KEY_PATH"
    printf '\n  %s%s%s\n' "$C_R$C_B" "$(t "!!! COPY THE BACKUP KEY OFF THIS MACHINE NOW: $key_note" "!!! YEDEK ANAHTARINI ŞİMDİ BU MAKİNENİN DIŞINA KOPYALAYIN: $key_note")" "$C_0"
    say "  !!! Without it no backup can ever be read. Store it in a password manager (with .env's GOOGLE_TOKEN_ENCRYPTION_KEY)." \
      "  !!! Onsuz hiçbir yedek okunamaz. Parola yöneticisinde saklayın (.env'deki GOOGLE_TOKEN_ENCRYPTION_KEY ile birlikte)."
    [ -z "$BACKUP_PUBKEY" ] || printf '  %s %s\n' "$(t 'Public key:' 'Açık anahtar:')" "$BACKUP_PUBKEY"
    say "  Backups are kept forever in BACKUP_HOST_DIR (default ./backups); copy them off the machine as well." \
      "  Yedekler BACKUP_HOST_DIR içinde (varsayılan ./backups) sonsuza dek saklanır; onları da makine dışına kopyalayın."
  fi
  if [ -n "$NOTES" ]; then
    say "" ""
    say "  Notes:" "  Notlar:"
    printf '%s' "$NOTES" | sed 's/^/  - /'
  fi
  say "" ""
  say "  Guide: docs/INSTALL.md (English), docs/INSTALL.tr.md (Türkçe). Re-running this installer is safe." \
    "  Rehber: docs/INSTALL.tr.md (Türkçe), docs/INSTALL.md (English). Kurulumu yeniden çalıştırmak güvenlidir."
}

# ---- main ----------------------------------------------------------------------------
main() {
  parse_args "$@"
  setup_colors
  if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
  fi
  if [ "$NONINTERACTIVE" = 0 ] && (: </dev/tty) 2>/dev/null; then
    exec 3<>/dev/tty
    INTERACTIVE=1
  else
    exec 3>&2
  fi
  detect_host
  choose_language
  printf '\n%sHealtOS installer %s%s\n' "$C_B" "$HEALTOS_INSTALLER_VERSION" "$C_0"
  say "Detected: $(host_label) ($HOST_ARCH)$([ "$IS_ROOT" = 1 ] && echo ', root')" \
    "Algılanan: $(host_label) ($HOST_ARCH)$([ "$IS_ROOT" = 1 ] && echo ', root')"
  if [ "$NONINTERACTIVE" = 0 ] && [ "$INTERACTIVE" = 0 ]; then
    say "No terminal for questions: continuing with flags and defaults." "Soru için terminal yok: seçenekler ve varsayılanlarla devam ediliyor."
  fi
  [ "$DETECTED" != unknown ] || [ "$DRY_RUN" = 1 ] || [ -n "$PLATFORM" ] \
    || die 2 "Unsupported operating system: $HOST_OS. Use Linux, macOS or Windows (install.ps1)." \
      "Desteklenmeyen işletim sistemi: $HOST_OS. Linux, macOS ya da Windows (install.ps1) kullanın."
  choose_platform
  resolve_dir
  # A bad --env-out fails here, before any question or output file.
  if [ "$DRY_RUN" = 1 ] && { [ -n "$ENV_OUT" ] || [ "$NEED_CLONE" = 0 ]; }; then resolve_env_out; fi
  preflight
  choose_mode
  choose_sources
  source_details
  choose_host_jobs
  confirm_plan
  clone_repo
  if [ "$NEED_CLONE" = 1 ] && [ "$DRY_RUN" = 1 ]; then
    say "  [dry-run] the .env preview needs a checkout; run again inside one (or after cloning)." \
      "  [dry-run] .env önizlemesi bir kopya ister; bir kopyanın içinde (ya da klonladıktan sonra) yeniden çalıştırın."
    exit 0
  fi
  write_env
  apple_token_setup
  garmin_share
  start_stack
  host_jobs
  summary
  if [ -n "$RENDER_TMP" ] && [ -f "$RENDER_TMP" ]; then rm -f "$RENDER_TMP"; fi
  exit 0
}

main "$@"
