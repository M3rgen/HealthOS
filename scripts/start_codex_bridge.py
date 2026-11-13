"""Start an authenticated Codex app-server for the local site.

Uses the installed Codex CLI and ChatGPT login. No API key is required.
Manual mode detaches and reuses a healthy bridge; --foreground supervises it.

The bridge never listens on all interfaces. CODEX_BRIDGE_BIND_HOST picks the
address; the default is the docker0 gateway on Linux Docker Engine (the address
`host.docker.internal:host-gateway` resolves to inside the app container) and
127.0.0.1 elsewhere (Docker Desktop forwards host.docker.internal to loopback).
Only loopback and Docker bridge addresses are accepted, unless
CODEX_BRIDGE_ALLOW_NON_LOCAL_BIND=1 keeps an address on another network working.
Firewall example for a Linux host: deploy/firewall/.

A configured token must have the strength of a generated one (app/core/secret_strength.py);
HEALTHOS_ALLOW_LEGACY_SECRETS=1 keeps one that only passes the previous rule.

Before the bridge starts, the selected Codex CLI binary is measured in an isolated, synthetic
probe (app/services/codex_runtime_probe.py): only the health tool may be reachable, image
reading, sub-agents and writes must be refused. A binary that is unknown or fails any check
never serves Umay; the rest of HealthOS is unaffected. The bridge runs a native binary only:
the first `codex` on PATH that is one (symlinks resolved; npm's codex.js is replaced by the
package's own binary; shell shims are skipped), or CODEX_BRIDGE_CLI, an absolute path.

The application reaches the bridge through scripts/codex_bridge_front.py, which proves its
identity with TLS (app/services/codex_bridge_tls.py); the app-server itself listens only on a
private loopback port, so no process can stand in for it.
"""

import argparse
import asyncio
import ipaddress
import json
import os
import platform
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.codex_bridge_service import occupied, supervise  # noqa: E402

PORT = 8766
ATTESTATION = "runtime-attestation.json"
MARKER = "front-marker"
ALLOW_NON_LOCAL_BIND = "CODEX_BRIDGE_ALLOW_NON_LOCAL_BIND"
ALLOW_LEGACY_SECRETS = "HEALTHOS_ALLOW_LEGACY_SECRETS"


def docker0_address():
    """IPv4 of the default Docker bridge on Linux, or None."""
    if not sys.platform.startswith("linux") or not shutil.which("ip"):
        return None
    result = subprocess.run(
        ["ip", "-4", "-o", "addr", "show", "dev", "docker0"],
        capture_output=True,
        text=True,
        check=False,
    )
    for field in result.stdout.split():
        if "/" in field:
            try:
                return str(ipaddress.IPv4Interface(field).ip)
            except ValueError:
                continue
    return None


def docker_bridge_addresses():
    """IPv4 of every Docker bridge on Linux: docker0 and the bridges Compose creates."""
    if not sys.platform.startswith("linux") or not shutil.which("ip"):
        return set()
    result = subprocess.run(
        ["ip", "-4", "-o", "addr", "show"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    addresses = set()
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) > 3 and (fields[1] == "docker0" or fields[1].startswith("br-")):
            try:
                addresses.add(str(ipaddress.IPv4Interface(fields[3]).ip))
            except ValueError:
                continue
    return addresses


def env_file_value(name):
    path = ROOT / ".env"
    if not path.exists():
        return ""
    try:
        text = path.read_text("utf-8")
    except OSError:
        # The service user cannot read the operator's chmod 600 .env; its settings
        # come from EnvironmentFile=/etc/healthos/bridge.env instead.
        return ""
    values = [
        value.strip().strip("\"'")
        for key, _, value in (line.partition("=") for line in text.splitlines())
        if key.strip() == name
    ]
    return values[-1] if values else ""


def option_enabled(name):
    """An operator's explicit yes, from the process environment or .env like the other settings."""
    value = os.environ.get(name, "").strip() or env_file_value(name)
    return value.strip().lower() in {"1", "true", "yes", "on"}


def bind_host():
    host = (
        os.environ.get("CODEX_BRIDGE_BIND_HOST", "").strip()
        or env_file_value("CODEX_BRIDGE_BIND_HOST")
        or docker0_address()
        or "127.0.0.1"
    )
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError as exc:
        raise SystemExit(f"CODEX_BRIDGE_BIND_HOST bir IP adresi olmalı: {host!r}") from exc
    # ::ffff:0.0.0.0 is every IPv4 interface; ipaddress does not call it unspecified.
    address = getattr(address, "ipv4_mapped", None) or address
    if address.is_unspecified:
        raise SystemExit(
            "Köprü tüm arayüzlerde dinleyemez; CODEX_BRIDGE_BIND_HOST için 127.0.0.1 "
            "ya da docker0 adresini kullan."
        )
    if not address.is_loopback and str(address) not in {
        docker0_address(),
        *docker_bridge_addresses(),
    }:
        if not option_enabled(ALLOW_NON_LOCAL_BIND):
            raise SystemExit(
                f"Köprü {address} adresinde dinleyemez: yalnız loopback ya da Docker köprü "
                "adresi kabul edilir. Bu adresi bilerek kullanıyorsan güvenlik duvarıyla "
                f"koruyup {ALLOW_NON_LOCAL_BIND}=1 ayarla."
            )
        print(
            f"Uyarı: köprü yerel olmayan {address} adresinde dinliyor "
            f"({ALLOW_NON_LOCAL_BIND}); 8766 portunu güvenlik duvarıyla sınırla.",
            file=sys.stderr,
        )
    return f"[{address}]" if address.version == 6 else str(address)


def write_private(path, text):
    """Replace a private file atomically; interrupted writes keep the previous identity."""
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def update_env(token):
    path = ROOT / ".env"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    replacements = {
        "CODEX_BRIDGE_TOKEN": token,
        "CODEX_BRIDGE_URL": os.environ.get("CODEX_BRIDGE_URL", "").strip()
        or env_file_value("CODEX_BRIDGE_URL")
        or f"wss://host.docker.internal:{PORT}",
    }
    lines = [line for line in lines if line.partition("=")[0].strip() not in replacements]
    write_private(
        path,
        "\n".join(lines + [f"{key}={value}" for key, value in replacements.items()]) + "\n",
    )


async def healthy(token, host):
    """Whether the bridge answers on ``host`` having proved its identity (the pinned TLS key)."""
    from websockets.asyncio.client import connect

    try:
        async with connect(
            f"wss://{host}:{PORT}",
            ssl=bridge_tls().client_context(token),
            open_timeout=2,
            additional_headers={"Authorization": f"Bearer {token}"},
            proxy=None,
        ) as ws:
            # The chat's own opening: the front lets no other through.
            params = policy_modules()[0].initialize_params()
            await ws.send(json.dumps({"id": 1, "method": "initialize", "params": params}))
            while True:
                reply = json.loads(await asyncio.wait_for(ws.recv(), 3))
                if reply.get("id") == 1:
                    return "result" in reply
    except Exception:
        return False


def policy_modules():
    """Bridge policy and probe live in the application package (stdlib-only modules)."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from app.services.umay import codex_bridge_policy, codex_runtime_probe

    return codex_bridge_policy, codex_runtime_probe


def bridge_tls():
    """The transport identity shared with the application."""
    policy_modules()
    from app.services.umay import codex_bridge_tls

    return codex_bridge_tls


def secret_strength():
    """The application's secret strength rule (a stdlib-only module)."""
    policy_modules()
    from app.core import secret_strength as strength

    return strength


def require_strong_token(token):
    """Refuse a configured token weaker than a generated one (the TLS key is derived from it)."""
    strength = secret_strength()
    if strength.strong_secret(token):
        return
    legacy = strength.legacy_strong_secret(token)
    if legacy and option_enabled(ALLOW_LEGACY_SECRETS):
        print(
            "Uyarı: CODEX_BRIDGE_TOKEN üretilmiş bir sır kadar güçlü değil "
            f"({ALLOW_LEGACY_SECRETS}); yenisini üretip değiştir.",
            file=sys.stderr,
        )
        return
    message = (
        "Umay köprüsü başlatılmadı: CODEX_BRIDGE_TOKEN üretilmiş bir sır kadar güçlü değil. "
        'Yenisini üret: python3 -c "import secrets; print(secrets.token_urlsafe(48))".'
    )
    if legacy:
        message += f" Geçici olarak korumak için {ALLOW_LEGACY_SECRETS}=1 ayarla."
    raise SystemExit(message)


def bridge_config(catalog_path):
    return policy_modules()[0].bridge_config(catalog_path)


def runtime_env(home, codex_home, tmpdir):
    """Environment of the served app-server: the probe's allowlist and provider transport."""
    policy_modules()
    from app.services.umay.codex_runtime_rpc import runtime_env as allowlisted

    return allowlisted(home, codex_home, tmpdir, network=True)


def file_digest(path):
    return policy_modules()[1].sha256_file(Path(path))


def policy_fingerprint():
    """Identifies the capability policy a running bridge was verified with."""
    return policy_modules()[0].policy_fingerprint()


def catalog_name():
    """The bridge model catalog's file name carries the policy fingerprint (chat checks it)."""
    return policy_modules()[0].catalog_file_name()


def free_loopback_port():
    """A currently free 127.0.0.1 port for the app-server behind the TLS front.

    Another process taking it first is caught: the app-server then cannot bind and exits, and
    the front never accepts a listener that does not know the launcher's random directory.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def private_marker(bridge_home):
    """Random directory name only this account can read; only the real app-server reports it."""
    path = bridge_home / MARKER
    try:
        marker = path.read_text(encoding="utf-8").strip()
    except OSError:
        marker = ""
    if not marker:
        marker = secrets.token_hex(24)
        write_private(path, marker)
    return marker


def listening(host):
    return occupied(host, PORT)


def stop_hint(bridge_home):
    """Operator instruction for a bridge process this launcher may not reuse."""
    pid_file = bridge_home / "bridge.pid"
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return "Bu adreste dinleyen süreci durdurup bu komutu yeniden çalıştır."
    return f"{pid_file} içindeki süreci (PID {pid}) durdurup bu komutu yeniden çalıştır."


# Leading bytes of native executables: ELF; Mach-O 32/64-bit in both byte orders; universal
# (fat) Mach-O. Windows: PE ("MZ"). Shell shims, npm's codex.js and other scripts are not
# native: hashing them would not measure the code that serves Umay.
POSIX_MAGIC = (
    b"\x7fELF",
    b"\xcf\xfa\xed\xfe",
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xfe\xed\xfa\xce",
    b"\xca\xfe\xba\xbe",
    b"\xca\xfe\xba\xbf",
)
# Target directories of the native binaries inside the npm package (vendor/<triple>/...).
NPM_TARGETS = {
    ("linux", "x86_64"): "x86_64-unknown-linux-musl",
    ("linux", "aarch64"): "aarch64-unknown-linux-musl",
    ("darwin", "x86_64"): "x86_64-apple-darwin",
    ("darwin", "aarch64"): "aarch64-apple-darwin",
    ("win32", "x86_64"): "x86_64-pc-windows-msvc",
    ("win32", "aarch64"): "aarch64-pc-windows-msvc",
}


def native_executable(path):
    """Whether ``path`` is a native binary (the code that runs), not a script or shim."""
    try:
        with Path(path).open("rb") as stream:
            head = stream.read(4)
    except OSError:
        return False
    if os.name == "nt":
        return head[:2] == b"MZ"
    return head in POSIX_MAGIC


def npm_target():
    """This host's directory name under the npm package's ``vendor``, or None."""
    machine = platform.machine().lower()
    machine = {"amd64": "x86_64", "arm64": "aarch64"}.get(machine, machine)
    system = "linux" if sys.platform.startswith("linux") else sys.platform
    return NPM_TARGETS.get((system, machine))


def npm_native_cli(launcher, bridge_home):
    """The platform's native binary inside an npm installation of the CLI, or None.

    npm's ``codex`` (a codex.js symlink, or codex.cmd on Windows) only starts that binary;
    the bridge runs and measures the binary itself.
    """
    resolved = launcher.resolve()
    packages = [bridge_home / "cli" / "node_modules" / "@openai" / "codex"]
    packages.append(launcher.parent / "node_modules" / "@openai" / "codex")
    if resolved.name == "codex.js" and resolved.parent.name == "bin":
        packages.insert(0, resolved.parent.parent)
    target = npm_target()
    name = "codex.exe" if os.name == "nt" else "codex"
    for package in packages:
        if target is None or not package.is_dir():
            continue
        found = sorted(
            {
                path.resolve()
                for path in package.rglob(name)
                if target in path.parts and "vendor" in path.parts and native_executable(path)
            }
        )
        if len(found) == 1:
            return found[0]
    return None


def native_cli(launcher, bridge_home):
    """The native binary behind ``launcher`` (itself, its symlink target or npm's), or None."""
    executable = Path(launcher).resolve()
    if native_executable(executable):
        return executable
    return npm_native_cli(Path(launcher).absolute(), bridge_home)


def select_cli(bridge_home):
    """The bridge's own CLI choice: CODEX_BRIDGE_CLI (absolute path) or the first `codex` on
    PATH that is (or, for npm, starts) a native binary; shell shims are skipped.

    The result is one native binary, symlinks resolved: the file the probe measures and
    hashes is the file that is executed, by the same absolute path.
    """
    configured = os.environ.get("CODEX_BRIDGE_CLI", "").strip()
    if configured:
        if not Path(configured).is_absolute() or not Path(configured).is_file():
            raise SystemExit("CODEX_BRIDGE_CLI var olan bir dosyanın mutlak yolu olmalı.")
        launchers = [configured]
    else:
        directories = os.environ.get("PATH", os.defpath).split(os.pathsep)
        launchers = [found for path in directories if (found := shutil.which("codex", path=path))]
    if not launchers:
        raise SystemExit("Codex CLI bulunamadı. Codex'in kurulu olduğu terminalden çalıştır.")
    for launcher in launchers:
        if executable := native_cli(launcher, bridge_home):
            return [str(executable)], executable
    raise SystemExit(
        "Umay köprüsü başlatılmadı: Codex CLI yerel bir ikili değil (kabuk betiği, shim ya da "
        "Node betiği). CODEX_BRIDGE_CLI ile yerel codex ikilisinin mutlak yolunu ver."
    )


def verify_runtime(command, executable, bridge_home):
    """Run the isolated capability probe; refuse to serve Umay on any failure."""
    _, probe = policy_modules()
    print("Umay köprüsünün çalışma zamanı güvenlik sınırları doğrulanıyor…", flush=True)
    try:
        report = probe.attest(command, executable, bridge_config, bridge_home / "probe")
    except probe.ProbeError as exc:
        details = exc.report or {}
        record = {key: value for key, value in details.items() if key != "catalog_text"}
        record.update(passed=False, error=str(exc), policy_sha256=policy_fingerprint())
        write_private(bridge_home / ATTESTATION, json.dumps(record, indent=2) + "\n")
        raise SystemExit(
            "Umay köprüsü başlatılmadı: Codex çalışma zamanı güvenlik doğrulamasını geçmedi "
            f"({exc}). Ayrıntı: {bridge_home / ATTESTATION}"
        ) from None
    record = {key: value for key, value in report.items() if key != "catalog_text"}
    record.update(policy_sha256=policy_fingerprint(), verified_at=int(time.time()))
    write_private(bridge_home / ATTESTATION, json.dumps(record, indent=2) + "\n")
    return report


def attested(bridge_home):
    """A running bridge may be reused only when it was verified under the current policy."""
    try:
        record = json.loads((bridge_home / ATTESTATION).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return record.get("passed") is True and record.get("policy_sha256") == policy_fingerprint()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--foreground", action="store_true", help="Run under a Linux service manager"
    )
    foreground = parser.parse_args().foreground
    if foreground and os.name != "posix":
        raise SystemExit("Ön planda servis çalıştırma yalnız POSIX sistemlerde desteklenir.")
    os.umask(0o077)
    host = bind_host()
    source_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    bridge_home = source_home / "healthos-bridge"
    bridge_home.mkdir(parents=True, exist_ok=True)
    bridge_home.chmod(0o700)
    token_file = bridge_home / "bridge.token"
    token = (
        os.environ.get("CODEX_BRIDGE_TOKEN", "").strip()
        or (token_file.read_text().strip() if token_file.exists() else "")
        or env_file_value("CODEX_BRIDGE_TOKEN")
    )
    if not token and foreground:
        raise SystemExit("Servis için CODEX_BRIDGE_TOKEN veya mevcut bridge.token gerekli.")
    if token:
        require_strong_token(token)
    else:
        token = secrets.token_urlsafe(48)
    if not foreground and asyncio.run(healthy(token, host)):
        if not attested(bridge_home):
            # Not stopped here: the operator decides about a process this launcher did not
            # verify. Until then the chat refuses it (config/read policy check).
            raise SystemExit(
                f"Çalışan Umay köprüsü ({host}:{PORT}) güncel güvenlik doğrulamasıyla "
                "başlatılmamış; Umay bu köprüyle sohbet açmaz. " + stop_hint(bridge_home)
            )
        update_env(token)
        print(f"HealthOS Codex köprüsü zaten çalışıyor ({host}:{PORT}).")
        if host != "127.0.0.1" and asyncio.run(healthy(token, "127.0.0.1")):
            print(
                "Uyarı: köprü loopback'te de yanıt veriyor; eski bir sürüm tüm arayüzlerde "
                f"dinliyor olabilir. {bridge_home / 'bridge.pid'} içindeki süreci durdurup "
                "bu komutu yeniden çalıştır."
            )
        return
    if listening(host):
        # An older bridge (another token, or the service starting next to a manual bridge)
        # would keep serving while a new one cannot bind: say so instead of a bind failure.
        raise SystemExit(
            f"{host}:{PORT} adresinde zaten bir süreç dinliyor; bu, doğrulanmamış eski bir Umay "
            "köprüsü olabilir ve Umay onunla sohbet açmaz. " + stop_hint(bridge_home)
        )
    # Measured before any login is copied: an unverified runtime never gets the identity.
    command, executable = select_cli(bridge_home)
    report = verify_runtime(command, executable, bridge_home)
    source_auth = source_home / "auth.json"
    # Separate app-server config avoids loading the desktop's plugins and MCP servers.
    bridge_auth = bridge_home / "auth.json"
    # A service restart must not replace credentials refreshed by the running bridge.
    if not foreground or not bridge_auth.exists():
        if not source_auth.exists():
            raise SystemExit("Önce codex login ile ChatGPT hesabına giriş yap.")
        write_private(bridge_auth, source_auth.read_text(encoding="utf-8"))
    bridge_auth.chmod(0o600)
    write_private(token_file, token)
    # The catalog's directory is the front's proof that it talks to this app-server: only the
    # real one reports it through config/read (scripts/codex_bridge_front.py).
    marker = private_marker(bridge_home)
    catalog_directory = bridge_home / f"verified-{marker}"
    catalog_directory.mkdir(exist_ok=True)
    catalog = catalog_directory / catalog_name()
    write_private(catalog, report["catalog_text"])
    write_private(bridge_home / "config.toml", bridge_config(str(catalog)))
    workspace = bridge_home / "workspace"
    workspace.mkdir(exist_ok=True)
    if file_digest(executable) != report["runtime"]["cli_sha256"]:
        raise SystemExit("Umay köprüsü başlatılmadı: Codex CLI doğrulamadan sonra değişti.")
    inner_token_file = bridge_home / "inner.token"
    write_private(inner_token_file, secrets.token_urlsafe(48))
    inner_port = free_loopback_port()
    command += [
        "app-server",
        "--listen",
        f"ws://127.0.0.1:{inner_port}",
        "--ws-auth",
        "capability-token",
        "--ws-token-file",
        str(inner_token_file),
    ]
    front_config = bridge_home / "front.json"
    write_private(
        front_config,
        json.dumps(
            {
                "home": str(bridge_home),
                "host": host,
                "port": PORT,
                "inner_port": inner_port,
                "marker": f"verified-{marker}",
                "command": command,
            }
        ),
    )
    command = [sys.executable, str(ROOT / "scripts" / "codex_bridge_front.py"), str(front_config)]
    # The probe's allowlist (HOME, CODEX_HOME, TMPDIR, LANG, PATH) plus the provider
    # transport: the served runtime sees what the measured one saw.
    home = bridge_home / "home"
    home.mkdir(exist_ok=True)
    child_env = runtime_env(home, bridge_home, Path(tempfile.gettempdir()))
    if foreground:
        return supervise(command, workspace, child_env, lambda: healthy(token, host))
    with (bridge_home / "bridge.log").open("ab") as log:
        process = subprocess.Popen(
            command,
            cwd=workspace,
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
        )
    for _ in range(40):
        if asyncio.run(healthy(token, host)):
            update_env(token)
            (bridge_home / "bridge.pid").write_text(str(process.pid), encoding="utf-8")
            print(f"HealthOS Codex köprüsü başlatıldı ({host}:{PORT}, ChatGPT aboneliği).")
            print("Siteyi güncelle: docker compose up -d --build app")
            print(f"Tanılama günlüğü: {bridge_home / 'bridge.log'}")
            return
        if process.poll() is not None:
            break
        time.sleep(0.3)
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    raise SystemExit(f"Codex köprüsü başlatılamadı. Günlük: {bridge_home / 'bridge.log'}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
