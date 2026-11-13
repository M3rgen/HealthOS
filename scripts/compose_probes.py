"""In-container probes and migration evidence for the isolated Compose drill."""

# Both smoke scripts run inside the app container; APP_UI_TOKEN (the throwaway token set in
# main()) comes from the Compose environment. Every route except /health/live and the login,
# webhook and OAuth-callback routes needs it, so each request sends it; the explicit
# "no token -> 401" probes are the only exceptions.
HEALTH_SMOKE_SCRIPT = "\n".join(
    [
        "import json, os, urllib.error, urllib.request",
        "base = 'http://127.0.0.1:8000'",
        "auth = {'Authorization': 'Bearer ' + os.environ['APP_UI_TOKEN']}",
        "assert urllib.request.urlopen(base + '/health/live', timeout=5).status == 200",
        "try:",
        "    urllib.request.urlopen(base + '/health', timeout=5)",
        "    raise SystemExit('/health answered without a bearer token')",
        "except urllib.error.HTTPError as error:",
        "    assert error.code == 401, error.code",
        "request = urllib.request.Request(base + '/health', headers=auth)",
        "result = json.load(urllib.request.urlopen(request, timeout=5))",
        "assert result['database'] == 'healthy', result",
        "assert result['service'] == 'healthos', result",
        # The flags below switch off reconciliation, the watchdog and the identity reconciler,
        # but the derived-day refresh always runs, so the scheduler must be up.
        "assert result['scheduler'] == 'running', result",
    ]
)

UI_SMOKE_SCRIPT = "\n".join(
    [
        "import json, os, re, urllib.error, urllib.request",
        "base = 'http://127.0.0.1:8000'",
        "auth = {'Authorization': 'Bearer ' + os.environ['APP_UI_TOKEN']}",
        "def get(path):",
        "    return urllib.request.urlopen(urllib.request.Request(base + path, headers=auth))",
        "html = get('/').read().decode()",
        "assert '<title>HealthOS' in html, html[:200]",
        'assets = re.findall(r\'(?:src|href)="(/assets/[^"]+)"\', html)',
        "assert len(assets) >= 2",
        "assert all(get(path).status == 200 for path in assets)",
        "try:",
        "    urllib.request.urlopen(base + '/api/dashboard')",
        "    raise SystemExit('/api/dashboard answered without a bearer token')",
        "except urllib.error.HTTPError as error:",
        "    assert error.code == 401, error.code",
        "data = json.load(get('/api/dashboard'))",
        "assert len(data['days']) == 7 and data['days'][0]['steps'] is None",
        "body = json.dumps({'metric': 'muscle', 'value': 42.5,",
        "                   'measured_at': '2026-09-29T09:00:00Z'}).encode()",
        "headers = {**auth, 'Content-Type': 'application/json', 'X-HealthOS-Request': '1'}",
        "request = urllib.request.Request(base + '/api/dashboard/measurements', data=body,",
        "                                 headers=headers, method='POST')",
        "assert urllib.request.urlopen(request).status == 201",
        "saved = json.load(get('/api/dashboard/measurements'))",
        "assert len(saved) == 1 and saved[0]['source'] == 'Manuel'",
        "data = json.load(get('/api/dashboard'))",
        "assert data['profile']['muscle']['value'] == 42.5",
        "csv = get('/api/dashboard/export.csv').read()",
        "assert b'sleep_seconds' in csv and b'UTC' in csv",
    ]
)


# Inspect ownership as well as access: Compose's read_only alone would hide a
# runtime-owned image when it is used standalone without a read-only rootfs.
APP_FILES_SMOKE_SCRIPT = "\n".join(
    [
        "import os, pathlib, tempfile",
        "assert os.getuid() == 10001",
        "root = pathlib.Path('/app')",
        "for path in [root, *root.rglob('*')]:",
        "    info = path.stat()",
        "    assert info.st_uid == 0 and not info.st_mode & 0o022, str(path)",
        "    assert not os.access(path, os.W_OK), str(path)",
        "tempfile.TemporaryFile(dir='/tmp').close()",
    ]
)


def verify_completed_migration(inspection: dict) -> None:
    state = inspection.get("State", {})
    if state.get("Status") != "exited" or state.get("ExitCode") != 0:
        raise RuntimeError("migrate: one-shot migration did not complete successfully")
    config = inspection.get("Config", {})
    if config.get("Cmd") != ["alembic", "upgrade", "head"]:
        raise RuntimeError("migrate: unexpected migration command")
    if "HEALTHOS_MIGRATE_ON_START=0" not in config.get("Env", []):
        raise RuntimeError("migrate: entrypoint migration must be disabled")


# Run inside the app container: what the application's own database login may and may not do.
# DATABASE_URL is the runtime URL Compose gives the app; EXPECTED_ROLE names the login the drill
# expects. In the opt-in case the owner password must not be in the container at all.
APP_ROLE_PROBE_SCRIPT = "\n".join(
    [
        "import os, psycopg",
        "url = os.environ['DATABASE_URL'].replace('+asyncpg', '').replace('+psycopg', '')",
        "assert os.environ.get('POSTGRES_PASSWORD', '') == ''",
        "conn = psycopg.connect(url, autocommit=True)",
        "row = conn.execute('SELECT current_user, rolsuper FROM pg_roles "
        "WHERE rolname = current_user').fetchone()",
        "assert row[0] == os.environ['EXPECTED_ROLE'] and row[1] is False, row",
        "def denied(statement):",
        "    try:",
        "        conn.execute(statement)",
        "    except psycopg.errors.InsufficientPrivilege:",
        "        return",
        "    raise SystemExit('application role was allowed: ' + statement)",
        "for statement in (",
        "    'ALTER TABLE raw_health_data DISABLE TRIGGER raw_health_data_no_delete',",
        "    'ALTER TABLE webhook_status_events DISABLE TRIGGER "
        "webhook_status_events_append_only',",
        "    'DROP TRIGGER webhook_status_events_append_only ON webhook_status_events',",
        "    'DROP TABLE alembic_version',",
        "    'TRUNCATE manual_measurements',",
        "    'CREATE TABLE should_not_exist (id integer)',",
        "    'CREATE ROLE should_not_exist',",
        "    'UPDATE alembic_version SET version_num = version_num',",
        "    \"COPY manual_measurements TO PROGRAM 'cat > /dev/null'\",",
        "):",
        "    denied(statement)",
        "try:",
        '    conn.execute("SET session_replication_role = replica")',
        "except psycopg.errors.InsufficientPrivilege:",
        "    pass",
        "else:",
        "    raise SystemExit('application role can set session_replication_role')",
        "print('application role proof passed')",
    ]
)

# Run with no database or network: the production settings guard against the mounted key.
GARMIN_KEY_PROBE_SCRIPT = "\n".join(
    [
        "import os, pathlib, stat",
        "from app.core.config import get_settings",
        "settings = get_settings()",
        "assert settings.garmin_enabled and 'garmin' in settings.healthos_sources",
        "path = pathlib.Path(os.environ['GARMIN_API_KEY_FILE'])",
        "assert os.getuid() == 10001 and path == settings.garmin_api_key_file",
        "assert path.read_text('utf-8').strip() == 'synthetic-garmin-key'",
        "info = path.stat()",
        "assert info.st_uid == 10001 and not info.st_mode & 0o077, oct(info.st_mode)",
    ]
)
