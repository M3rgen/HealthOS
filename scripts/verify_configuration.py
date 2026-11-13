"""Non-destructive repository and environment configuration checks."""

from __future__ import annotations

import argparse
import re
import shlex
import subprocess
from pathlib import Path

import yaml

from app.core.sources import readable_garmin_key, safe_database_url, selected_sources

ROOT = Path(__file__).resolve().parents[1]
PLACEHOLDERS = {"change_me", "changeme", "todo", "replace_me", "example-secret"}
REQUIRED_PRODUCTION = {"APP_UI_TOKEN", "POSTGRES_PASSWORD", "DATABASE_URL"}
GOOGLE_REQUIRED = {
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "GOOGLE_TOKEN_ENCRYPTION_KEY",
    "GOOGLE_HEALTH_WEBHOOK_ENDPOINT_URI",
    "GOOGLE_HEALTH_WEBHOOK_AUTHORIZATION",
}


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        values[name.strip()] = value.strip().strip("\"'")
    return values


def check_environment(path: Path) -> list[str]:
    values = parse_env(path)
    errors: list[str] = []
    try:
        sources = selected_sources(
            values.get("HEALTHOS_SOURCES", "google"),
            legacy_garmin="HEALTHOS_SOURCES" not in values
            and values.get("GARMIN_ENABLED", "false").lower() in {"true", "1", "yes", "on"},
        )
    except ValueError as exc:
        return [str(exc)]
    required = REQUIRED_PRODUCTION | (GOOGLE_REQUIRED if "google" in sources else set())
    for name in sorted(required):
        value = values.get(name, "").strip()
        if not value:
            errors.append(f"{name}: missing or empty")
        elif value.casefold() in PLACEHOLDERS or "change_me" in value.casefold():
            errors.append(f"{name}: placeholder value")
    if values.get("POSTGRES_PASSWORD") in {"health", "postgres", "password"}:
        errors.append("POSTGRES_PASSWORD: known insecure default")
    app_user, app_password = (
        values.get("POSTGRES_APP_USER", ""),
        values.get("POSTGRES_APP_PASSWORD", ""),
    )
    if app_user or app_password:
        if not (app_user and app_password):
            errors.append("POSTGRES_APP_USER/POSTGRES_APP_PASSWORD: set both, or neither")
        elif not re.fullmatch(r"[A-Za-z0-9_.~-]{24,512}", app_password):
            errors.append("POSTGRES_APP_PASSWORD: must be 24-512 URL-safe characters")
    endpoint = values.get("GOOGLE_HEALTH_WEBHOOK_ENDPOINT_URI", "")
    if "google" in sources and endpoint and not endpoint.startswith("https://"):
        errors.append("GOOGLE_HEALTH_WEBHOOK_ENDPOINT_URI: must use HTTPS")
    if values.get("DATABASE_URL") and not safe_database_url(values["DATABASE_URL"]):
        errors.append("DATABASE_URL: password missing or known insecure default")
    if "garmin" in sources and not readable_garmin_key(
        Path(values.get("GARMIN_API_KEY_FILE", "~/.config/garmin/connect-personal-api-key"))
    ):
        errors.append("GARMIN_API_KEY_FILE: missing, empty or unreadable")
    return errors


def source_head() -> str:
    revisions: dict[str, set[str]] = {}
    for path in (ROOT / "migrations" / "versions").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        revision = re.search(r'^revision = "([^"]+)"', text, re.MULTILINE)
        # A merge revision names a tuple of parents, possibly over several lines.
        parent = re.search(r'^down_revision = (\([^)]*\)|"[^"]+"|None)', text, re.MULTILINE)
        if revision and parent:
            revisions[revision.group(1)] = set(re.findall(r'"([^"]+)"', parent.group(1)))
    parents = {parent for found in revisions.values() for parent in found}
    heads = sorted(set(revisions) - parents)
    if len(heads) != 1:
        raise RuntimeError(f"expected one Alembic head; found {heads}")
    return heads[0]


def compose_service(document: object, name: str) -> dict:
    services = document.get("services") if isinstance(document, dict) else None
    service = services.get(name) if isinstance(services, dict) else None
    return service if isinstance(service, dict) else {}


def service_environment(service: dict) -> dict[str, str]:
    environment = service.get("environment") or {}
    if isinstance(environment, list):
        pairs = (item.split("=", 1) for item in environment if "=" in item)
        return {name: value for name, value in pairs}
    return {str(name): str(value) for name, value in environment.items()}


def compose_boolean(value: object, expected: bool) -> bool:
    """Compose accepts booleans and their quoted string equivalents."""
    return value is expected or (isinstance(value, str) and value.lower() == str(expected).lower())


def has_no_new_privileges(options: object) -> bool:
    if not isinstance(options, list):
        return False
    values = [
        re.split("[:=]", option, maxsplit=1)
        for option in options
        if isinstance(option, str)
        and re.split("[:=]", option, maxsplit=1)[0] == "no-new-privileges"
    ]
    return bool(values) and all(len(parts) == 1 or parts[1] == "true" for parts in values)


def compose_mounts(service: dict) -> list[dict]:
    """Normalize short/long volumes and scalar/list tmpfs without needing Docker."""
    mounts: list[dict] = []
    tmpfs = service.get("tmpfs") or []
    entries = [tmpfs] if isinstance(tmpfs, str) else tmpfs if isinstance(tmpfs, list) else []
    for entry in entries:
        if isinstance(entry, str):
            target, _, options = entry.partition(":")
            flags = dict(item.partition("=")[::2] for item in options.split(","))
            mounts.append(
                {
                    "type": "tmpfs",
                    "target": target,
                    "read_only": "ro" in flags,
                    "mode": flags.get("mode", "1777"),
                }
            )
    volumes = service.get("volumes") or []
    for entry in volumes if isinstance(volumes, list) else []:
        if isinstance(entry, dict):
            mount = dict(entry)
            options = mount.get("tmpfs") or {}
            mount["mode"] = options.get("mode", 0o1777) if isinstance(options, dict) else 0
            mounts.append(mount)
        elif isinstance(entry, str):
            parts = entry.split(":")
            source, target = parts[:2] if len(parts) > 1 else (None, parts[0])
            mounts.append(
                {
                    "type": "bind" if source and source.startswith(("/", ".", "~")) else "volume",
                    "source": source,
                    "target": target,
                    "read_only": "ro" in (parts[2].split(",") if len(parts) > 2 else []),
                }
            )
    return mounts


def check_service_hardening(name: str, service: dict) -> list[str]:
    errors: list[str] = []
    if not compose_boolean(service.get("read_only"), True):
        errors.append(f"{name} service must set read_only: true")
    if not has_no_new_privileges(service.get("security_opt")):
        errors.append(f"{name} service must enable no-new-privileges")
    if "ALL" not in (service.get("cap_drop") or []):
        errors.append(f"{name} service must drop all Linux capabilities (cap_drop: [ALL])")
    if not service.get("mem_limit") or not service.get("pids_limit"):
        errors.append(f"{name} service must set a memory limit and a PID limit")
    mounts = compose_mounts(service)
    temporary = ["/tmp", "/var/run/postgresql"] if name == "postgres" else ["/tmp"]
    for target in temporary:
        matching = [mount for mount in mounts if mount.get("target") == target]
        if len(matching) != 1 or matching[0].get("type") != "tmpfs":
            errors.append(f"{name} service requires a writable tmpfs at {target}")
            continue
        mount = matching[0]
        try:
            mode = mount.get("mode", 0o1777)
            mode = int(mode, 8) if isinstance(mode, str) else int(mode)
        except (ValueError, TypeError):
            mode = 0
        # /tmp is shared by unprivileged processes. The postgres entrypoint
        # chowns/chmods its socket directory before switching users.
        permissions = mode & 0o1777 == 0o1777 if target == "/tmp" else mode & 0o700 == 0o700
        if not compose_boolean(mount.get("read_only", False), False) or not permissions:
            errors.append(f"{name} service tmpfs {target} must be writable with usable permissions")
    data_target = "/var/lib/postgresql/data"
    if name == "postgres":
        data = [mount for mount in mounts if mount.get("target") == data_target]
        if len(data) != 1 or not (
            data[0].get("type") == "volume"
            and data[0].get("source") == "postgres_data"
            and compose_boolean(data[0].get("read_only", False), False)
            and not (data[0].get("volume") or {}).get("subpath")
        ):
            errors.append(f"postgres service requires writable postgres_data at {data_target}")
    allowed = {
        *temporary,
        *([data_target] if name == "postgres" else []),
        *(["/data", "/config"] if name == "caddy" else []),
    }
    for mount in mounts:
        target = str(mount.get("target", ""))
        if target not in allowed and (
            not compose_boolean(mount.get("read_only", False), True)
            or any(target.startswith(path + "/") for path in allowed)
        ):
            errors.append(f"{name} service has unexpected mount at {target}")
    return errors


def check_compose_runtime(compose_doc: object, dev_doc: object) -> list[str]:
    errors: list[str] = []
    app = compose_service(compose_doc, "app")
    if service_environment(app).get("APP_ENV") != "production":
        errors.append("docker-compose.yml app service must set APP_ENV: production")
    errors.extend(check_compose_services(compose_doc))
    dev_app = compose_service(dev_doc, "app")
    if service_environment(dev_app).get("APP_ENV") != "development":
        errors.append("docker-compose.dev.yml must override APP_ENV with development")
    migrate = compose_service(compose_doc, "migrate")
    if not migrate:
        errors.append("docker-compose.yml must define a migrate service")
    elif service_environment(migrate).get("APP_ENV") != "production":
        errors.append("migrate service must set APP_ENV: production")
    # A missing base service is a production error, not a broken dev override.
    if (
        migrate
        and service_environment(compose_service(dev_doc, "migrate")).get("APP_ENV") != "development"
    ):
        errors.append("docker-compose.dev.yml migrate must override APP_ENV with development")
    if (app.get("depends_on") or {}).get("migrate") != {
        "condition": "service_completed_successfully"
    }:
        errors.append("app must depend on successful migrate completion")
    if (migrate.get("depends_on") or {}).get("postgres") != {"condition": "service_healthy"}:
        errors.append("migrate must wait for healthy postgres")
    if migrate.get("command") != ["alembic", "upgrade", "head"]:
        errors.append("migrate must run alembic upgrade head")
    # The container always listens on 8000; .env's APP_PORT only picks the published host port.
    if service_environment(app).get("APP_PORT") != "8000":
        errors.append('app must pin APP_PORT: "8000" inside the container')
    if not re.search(r"://health:", str(service_environment(migrate).get("DATABASE_URL", ""))):
        errors.append("migrate must connect as the schema owner")
    provision = compose_service(compose_doc, "provision-app-role")
    if (provision.get("depends_on") or {}).get("migrate") != {
        "condition": "service_completed_successfully"
    } or (app.get("depends_on") or {}).get("provision-app-role") != {
        "condition": "service_completed_successfully"
    }:
        errors.append("the application role must be provisioned after migrations, before the app")
    if migrate.get("restart") != "no" or migrate.get("healthcheck") != {"disable": True}:
        errors.append("migrate must be one-shot with its HTTP healthcheck disabled")
    errors.extend(check_compose_build(compose_doc))
    if migrate.get("env_file") != app.get("env_file"):
        errors.append("migrate must share the app env_file")
    for name, service in (("app", app), ("migrate", migrate)):
        if service_environment(service).get("HEALTHOS_MIGRATE_ON_START") != "0":
            errors.append(f"{name} entrypoint migration must be disabled in Compose")
    return errors


def check_compose_build(compose_doc: object) -> list[str]:
    errors: list[str] = []
    services = compose_doc.get("services", {}) if isinstance(compose_doc, dict) else {}
    if [name for name, service in services.items() if "build" in service] != ["app"]:
        errors.append("only app must build the shared application image")
    if compose_service(compose_doc, "app").get("build") != ".":
        errors.append("app must build from the repository Dockerfile")
    for name in ("app", "migrate", "provision-app-role"):
        if compose_service(compose_doc, name).get("image") != (
            "${COMPOSE_PROJECT_NAME:-healthos}-app:latest"
        ):
            errors.append(f"{name} must use the project-scoped shared application image")
    return errors


def check_compose_services(compose_doc: object) -> list[str]:
    errors: list[str] = []
    for name in ("postgres", "app", "migrate", "provision-app-role", "caddy"):
        service = compose_service(compose_doc, name)
        errors.extend(check_service_hardening(name, service))
    for name in ("postgres", "app", "migrate"):
        service = compose_service(compose_doc, name)
        logging = service.get("logging") or {}
        options = logging.get("options") or {}
        if (
            logging.get("driver") != "json-file"
            or not options.get("max-size")
            or not options.get("max-file")
        ):
            errors.append(f"{name} service lacks json-file log rotation (max-size, max-file)")
    return errors


def check_small_hardening() -> list[str]:
    errors: list[str] = []
    keyset_default = re.search(
        r'google_health_webhook_public_key_url: str = \(\s*"([^"]+)"',
        (ROOT / "app" / "core" / "config.py").read_text(encoding="utf-8"),
    )
    if not keyset_default or not keyset_default.group(1).startswith("https://www.gstatic.com/"):
        errors.append("the default webhook keyset URL must be on https://www.gstatic.com/")
    template_keyset = parse_env(ROOT / ".env.example").get("GOOGLE_HEALTH_WEBHOOK_PUBLIC_KEY_URL")
    if template_keyset and not template_keyset.startswith("https://www.gstatic.com/"):
        errors.append(".env.example webhook keyset URL must be on https://www.gstatic.com/")
    alembic_url = re.search(
        r"^sqlalchemy\.url\s*=(.*)$",
        (ROOT / "alembic.ini").read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    if alembic_url and alembic_url.group(1).strip():
        errors.append("alembic.ini sqlalchemy.url must be empty; env.py sets it from DATABASE_URL")
    return errors


def check_backup_automation(compose_doc: object, verifier: str) -> list[str]:
    errors: list[str] = []
    systemd = ROOT / "deploy" / "systemd"
    timer = systemd / "healthos-backup.timer"
    service = systemd / "healthos-backup.service"
    if not timer.is_file() or "Persistent=true" not in timer.read_text(encoding="utf-8"):
        errors.append("deploy/systemd/healthos-backup.timer is missing or not Persistent=true")
    if not service.is_file() or "scripts/backup.sh" not in service.read_text(encoding="utf-8"):
        errors.append("deploy/systemd/healthos-backup.service must run scripts/backup.sh")
    for script in ("backup.sh", "restore.sh"):
        if not (ROOT / "scripts" / script).is_file():
            errors.append(f"scripts/{script} is missing")
    backup_script = (ROOT / "scripts" / "backup.sh").read_text(encoding="utf-8")
    if "age --encrypt" not in backup_script or "gpg " not in backup_script:
        errors.append("scripts/backup.sh must encrypt dumps with age or gpg")
    if compose_service(compose_doc, "backup").get("volumes"):
        errors.append("the backup service must stream to scripts/backup.sh, not write to a volume")
    app = compose_service(compose_doc, "app")
    expected_mount = {
        "type": "bind",
        "source": "${BACKUP_STATUS_HOST_DIR:-./backup-status}",
        "target": "/run/healthos-backup",
        "read_only": True,
        # An existing server updated before creating the directory must still start.
        "bind": {"create_host_path": True},
    }
    mounts = compose_mounts(app)
    data_mounts = [mount for mount in mounts if mount.get("type") != "tmpfs"]
    if [{key: mount.get(key) for key in expected_mount} for mount in data_mounts] != [
        expected_mount
    ]:
        errors.append("app must mount only the read-only backup metadata directory")
    if service_environment(app).get("BACKUP_STATUS_FILE") != "/run/healthos-backup/status.json":
        errors.append("app BACKUP_STATUS_FILE must select the metadata file")
    if "restore.sh" not in verifier or "backup.sh" not in verifier:
        errors.append("scripts/verify_compose.py must exercise backup.sh and restore.sh")
    return errors


# Built from parts so this file does not match its own rule.
WILDCARD_BRIDGE = "0.0.0.0" + ":8766"
BRIDGE_SCAN_GLOBS = (
    "*.yml",
    "*.md",
    ".env.example",
    "Dockerfile",
    "Makefile",
    "app/**/*.py",
    "scripts/*",
    "deploy/**/*",
    "docs/**/*.md",
)


def repository_files() -> list[Path]:
    """Tracked files (so local untracked notes are ignored); globbing outside Git."""
    try:
        listed = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
        ).stdout.decode()
    except (OSError, subprocess.CalledProcessError):
        return sorted({path for pattern in BRIDGE_SCAN_GLOBS for path in ROOT.glob(pattern)})
    tracked = {ROOT / name for name in listed.split("\0") if name}
    return sorted({path for pattern in BRIDGE_SCAN_GLOBS for path in ROOT.glob(pattern)} & tracked)


def check_codex_bridge_binding() -> list[str]:
    errors: list[str] = []
    for path in repository_files():
        if path.is_file():
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            if WILDCARD_BRIDGE in text:
                relative = path.relative_to(ROOT).as_posix()
                errors.append(f"{relative}: the Codex bridge must not listen on all interfaces")
    bridge = (ROOT / "scripts" / "start_codex_bridge.py").read_text(encoding="utf-8")
    if "CODEX_BRIDGE_BIND_HOST" not in bridge:
        errors.append("scripts/start_codex_bridge.py must take its bind address from the env")
    if not (ROOT / "deploy" / "firewall" / "codex-bridge.nft").is_file():
        errors.append("deploy/firewall/codex-bridge.nft example is missing")
    return errors


DOCUMENTATION_GLOBS = ("*.md", "docs/**/*.md")
FENCED_BLOCK = re.compile(r"^ {0,3}(`{3,}|~{3,}).*?^ {0,3}\1[ \t]*$", re.MULTILINE | re.DOTALL)
INLINE_CODE = re.compile(r"(`+)(?!`).+?(?<!`)\1", re.DOTALL)
# `](target)` covers links, images and badges nested in links; an optional title follows.
MARKDOWN_TARGET = re.compile(r"\]\(\s*<?([^()\s<>]+)>?(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)")
HTML_TARGET = re.compile(r"<(?:img|a|source)\b[^>]*?\s(?:src|href)=[\"']([^\"']+)[\"']", re.I)
HEADING = re.compile(r"^ {0,3}#{1,6}[ \t]+(.+?)[ \t#]*$", re.MULTILINE)


def documentation_files(root: Path = ROOT) -> list[Path]:
    return sorted({path for pattern in DOCUMENTATION_GLOBS for path in root.glob(pattern)})


def strip_code(markdown: str) -> str:
    """Drop fenced blocks and inline code so example syntax is not read as a link."""
    return INLINE_CODE.sub("", FENCED_BLOCK.sub("", markdown))


def heading_anchors(markdown: str) -> set[str]:
    """GitHub-style heading slugs, including the -1, -2 suffixes of repeated headings."""
    anchors: set[str] = set()
    seen: dict[str, int] = {}
    for heading in HEADING.findall(FENCED_BLOCK.sub("", markdown)):
        text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", heading)  # keep link text only
        text = re.sub(r"<[^>]+>", "", text).replace("`", "").strip().lower()
        slug = re.sub(r"[^\w\- ]", "", text).replace(" ", "-")
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        anchors.add(slug if count == 0 else f"{slug}-{count}")
    return anchors


def broken_links(markdown: Path, root: Path = ROOT) -> list[str]:
    """Relative links and image references in one Markdown file that do not resolve.

    External (scheme) links and pure `#anchor` links are ignored. `file.md#anchor`
    must name an existing file and, for Markdown targets, an existing heading.
    """
    content = strip_code(markdown.read_text(encoding="utf-8"))
    targets = [*MARKDOWN_TARGET.findall(content), *HTML_TARGET.findall(content)]
    problems: list[str] = []
    for target in targets:
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target) or target.startswith(("#", "//")):
            continue
        path_part, _, fragment = target.partition("#")
        path_part = path_part.split("?", 1)[0]
        resolved = (markdown.parent / path_part).resolve()
        label = f"{markdown.relative_to(root).as_posix()}: {target}"
        if not resolved.exists():
            problems.append(f"broken documentation link in {label}")
        elif fragment and resolved.suffix == ".md" and resolved.is_file():
            if fragment.lower() not in heading_anchors(resolved.read_text(encoding="utf-8")):
                problems.append(f"broken documentation anchor in {label}")
    return problems


def check_documentation_links(root: Path = ROOT) -> list[str]:
    errors: list[str] = []
    for markdown in documentation_files(root):
        errors.extend(broken_links(markdown, root))
    return errors


def check_caddy_routes(text: str) -> list[str]:
    """Fail closed unless every proxy uses an exact webhook-only path matcher.

    Parse the deployment's supported Caddy directive subset, including scope and
    matcher references. Unknown routing constructs need an explicit review here.
    """
    error = ["Caddy must proxy only /webhooks/google-health and deny unmatched routes"]
    scope: list[str] = []
    matchers: dict[str, list[str]] = {}
    proxies: list[str] = []
    sites, deny = 0, False
    try:
        for line in text.splitlines():
            words = shlex.split(line, comments=True)
            if not words:
                continue
            if words[-1] == "{":
                if not scope and len(words) == 2:
                    sites += 1
                    scope.append("site")
                elif scope == ["site"] and words[0] == "handle" and len(words) in {2, 3}:
                    scope.append(words[1] if len(words) == 3 else "")
                else:
                    return error
            elif words == ["}"] and scope:
                scope.pop()
            elif (
                scope == ["site"]
                and len(words) >= 3
                and words[0].startswith("@")
                and words[1] == "path"
                and words[0] not in matchers
            ):
                matchers[words[0]] = words[2:]
            elif len(scope) == 2 and words[0] == "reverse_proxy" and len(words) == 2:
                proxies.append(scope[-1])
            elif scope == ["site", ""] and words == ["respond", "404"]:
                deny = True
            else:
                return error
    except ValueError:
        return error
    if scope or sites != 1 or not deny or not proxies:
        return error
    if any(set(matchers.get(guard, [])) != {"/webhooks/google-health"} for guard in proxies):
        return error
    return []


def check_compose_placeholders() -> list[str]:
    return [
        f"{path.name} is a Compose file containing only services: {{}}"
        for path in sorted(ROOT.glob("docker-compose*.yml"))
        if yaml.safe_load(path.read_text(encoding="utf-8")) == {"services": {}}
    ]


def check_repository() -> list[str]:
    errors: list[str] = []
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    compose_doc = yaml.safe_load(compose)
    dev_doc = yaml.safe_load((ROOT / "docker-compose.dev.yml").read_text(encoding="utf-8"))
    errors.extend(check_compose_runtime(compose_doc, dev_doc))
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    forbidden = ("POSTGRES_PASSWORD:-health", "postgresql://health:health@postgres")
    for value in forbidden:
        if value in compose or value in dockerfile:
            errors.append(f"insecure fallback remains: {value}")
    # The three local services use the one app build, checked by check_compose_build.
    # Only pulled images have an upstream digest and an IMAGE_UPDATES entry.
    image_lines = [
        service["image"]
        for name, service in compose_doc["services"].items()
        if name not in {"app", "migrate", "provision-app-role"} and "image" in service
    ]
    if not image_lines or any("@sha256:" not in image for image in image_lines):
        errors.append("every external Compose image must have a digest")
    from_lines = re.findall(r"^FROM\s+(\S+)", dockerfile, re.MULTILINE)
    if any("@sha256:" not in image for image in from_lines):
        errors.append("every Dockerfile base image must have a digest")
    if '"127.0.0.1:${APP_PORT:-8000}:8000"' not in compose:
        errors.append("the administrative API is not explicitly loopback-only")
    postgres_block = compose.split("  app:", 1)[0]
    if "ports:" in postgres_block:
        errors.append("PostgreSQL unexpectedly has a base Compose port mapping")
    errors.extend(check_compose_placeholders())
    expected = source_head()
    verifier = (ROOT / "scripts" / "verify_compose.py").read_text(encoding="utf-8")
    if f'EXPECTED_REVISION = "{expected}"' not in verifier:
        errors.append("Compose verifier Alembic head is stale")
    if '"WATCHDOG_ENABLED": "false"' not in verifier:
        errors.append("Compose verifier must explicitly disable all schedulers")
    errors.extend(check_backup_automation(compose_doc, verifier))
    errors.extend(check_codex_bridge_binding())
    errors.extend(check_small_hardening())
    cloudflare = (ROOT / "deploy" / "cloudflare" / "config.example.yml").read_text(encoding="utf-8")
    if "http_status:404" not in cloudflare:
        errors.append("Cloudflare example lacks a deny-by-default final rule")
    caddy = (ROOT / "Caddyfile").read_text(encoding="utf-8")
    errors.extend(check_caddy_routes(caddy))
    image_docs = (ROOT / "docs" / "IMAGE_UPDATES.md").read_text(encoding="utf-8")
    for image in (*image_lines, *from_lines):
        if image not in image_docs:
            errors.append(f"pinned image is missing from update documentation: {image}")
    template = parse_env(ROOT / ".env.example")
    for name in (
        REQUIRED_PRODUCTION
        | GOOGLE_REQUIRED
        | {"HEALTHOS_SOURCES", "GOOGLE_TOKEN_ENCRYPTION_KEYS_OLD"}
    ):
        if name not in template:
            errors.append(f".env.example omits {name}")
    if any(value.casefold() in PLACEHOLDERS for value in template.values()):
        errors.append(".env.example contains an accepted placeholder secret")
    errors.extend(check_documentation_links())
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--env-file",
        type=Path,
        help="validate a production env file without printing values",
    )
    args = parser.parse_args()
    errors = check_environment(args.env_file) if args.env_file else check_repository()
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("Configuration validation passed; no secret values were printed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
