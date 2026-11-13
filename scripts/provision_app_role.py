"""Create or update the non-superuser role the application connects with at runtime.

Run once after every ``alembic upgrade`` (the ``provision-app-role`` Compose service does). The
schema stays owned by the migration role: the application role gets data access only. Owning no
table it cannot ``DISABLE TRIGGER``, ``DROP TRIGGER``, ``TRUNCATE`` or alter anything, and without
superuser it cannot ``SET session_replication_role`` or use ``COPY ... PROGRAM``, so the append-only
guards hold against the application itself. (They are ``ENABLE ALWAYS``, so replica mode would not
bypass them anyway.)

Everything here is additive: ``CREATE ROLE`` and ``GRANT`` (and ``ALTER ROLE`` to set the
password), nothing is dropped and no record contents are read or changed. Without
``POSTGRES_APP_USER`` and ``POSTGRES_APP_PASSWORD`` the script connects only to check whether
archive records or a prior startup exist, distinguishing new installs from existing ones.
New installs are refused; existing installs keep the owner login until the operator opts in
(see docs/DEPLOYMENT.md). ``POSTGRES_ALLOW_OWNER_RUNTIME=1`` explicitly skips this check.

The password is hashed here into a SCRAM verifier, so the clear text never reaches the server or
a statement log.
"""

from __future__ import annotations

import os
import re
import sys

import psycopg
from psycopg import sql

from app.core.role_policy import PRIVILEGED_ATTRIBUTES, borrowed_privilege_sql
from app.core.secret_strength import legacy_strong_secret, strong_secret

# Maintenance the application does itself: statistics after an identity backfill
# (app/services/identity_backfill*.py). MAINTAIN needs PostgreSQL 17 and grants ANALYZE, VACUUM
# and friends only; never ALTER TABLE. Older servers only warn and skip the ANALYZE.
ANALYZED_TABLES = (
    "raw_health_data_identity",
    "raw_health_data_supersessions",
    "raw_source_chains",
    "raw_source_sightings",
    "raw_source_devices",
)
# Everything an install archives itself. All empty means no install has ever had data here.
ARCHIVE_TABLES = (
    "raw_health_data",
    "manual_measurements",
    "manual_workouts",
    "garmin_api_responses",
    "garmin_raw_records",
    "garmin_record_observations",
    "garmin_heart_sets",
    "garmin_heart_members",
    "garmin_daily_projections",
    "garmin_activity_results",
)
ROLE_PATTERN = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
PASSWORD_PATTERN = re.compile(r"^[A-Za-z0-9_.~-]{24,512}$")
ROLE_FLAGS = "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS"
# NOINHERIT does not stop ``SET ROLE``: a member of one of these roles can take its rights back.
# The owner (it can disable the guards), any role with a privileged attribute, any role owning a
# table here, and the built-in roles that reach the server's files and programs.
BORROWED_PRIVILEGE = borrowed_privilege_sql("%(user)s", "%(owner)s")


class ProvisionError(RuntimeError):
    """An operator error, reported without a traceback."""


def requested_role(user: str, password: str, *, owner: str, allow_legacy: bool = False) -> bool:
    """True when a role is configured; False for the unchanged (opt-out) install."""
    if not user and not password:
        return False
    if not user or not password:
        raise ProvisionError("set POSTGRES_APP_USER and POSTGRES_APP_PASSWORD together, or neither")
    if not ROLE_PATTERN.fullmatch(user):
        raise ProvisionError("POSTGRES_APP_USER must be a plain lower-case identifier")
    if user in {owner, "postgres"}:
        raise ProvisionError("POSTGRES_APP_USER must not name a privileged or owner role")
    if not PASSWORD_PATTERN.fullmatch(password):
        raise ProvisionError(
            "POSTGRES_APP_PASSWORD must be 24-512 URL-safe characters; generate one with: "
            'python3 -c "import secrets; print(secrets.token_hex(32))"'
        )
    if not strong_secret(password):
        # The rule every other production secret meets (app/core/secret_strength.py).
        if not (allow_legacy and legacy_strong_secret(password)):
            raise ProvisionError(
                "POSTGRES_APP_PASSWORD is weaker than a generated secret (32+ characters, no "
                "repeated, sequential or common pattern); generate one with: "
                'python3 -c "import secrets; print(secrets.token_hex(32))"'
            )
        print(
            "provision-app-role: HEALTHOS_ALLOW_LEGACY_SECRETS keeps a POSTGRES_APP_PASSWORD "
            "that passes only the earlier rule; replace it with a generated one",
            file=sys.stderr,
        )
    return True


def holds_archive(conn: psycopg.Connection) -> bool:
    """Whether any archive table has a row: an existing install, not a new one."""
    with conn.cursor() as cur:
        for table in ARCHIVE_TABLES:
            cur.execute("SELECT to_regclass(%s)", (f"public.{table}",))
            row = cur.fetchone()
            if row is None or row[0] is None:
                continue
            cur.execute(sql.SQL("SELECT EXISTS (SELECT 1 FROM {})").format(sql.Identifier(table)))
            present = cur.fetchone()
            if present is not None and present[0]:
                return True
    return False


def application_has_run(conn: psycopg.Connection) -> bool:
    """Whether an application process started on this database before.

    Every startup records ``monitoring_started`` here. A new install cannot have a row: the
    application only starts after this script succeeds. An older install can, even empty.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.webhook_status_events')")
        row = cur.fetchone()
        if row is None or row[0] is None:
            return False
        cur.execute("SELECT EXISTS (SELECT 1 FROM webhook_status_events)")
        present = cur.fetchone()
        return present is not None and bool(present[0])


def provision(conn: psycopg.Connection, user: str, password: str) -> None:
    """Create or update ``user`` with the grants a running application needs."""
    role = sql.Identifier(user)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT current_database(), current_user, current_setting('server_version_num')"
        )
        row = cur.fetchone()
        assert row is not None
        database, owner, version = row
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (user,))
        exists = cur.fetchone() is not None
        verifier = conn.pgconn.encrypt_password(
            password.encode(), user.encode(), b"scram-sha-256"
        ).decode()
        action = "ALTER" if exists else "CREATE"
        cur.execute(
            sql.SQL("{} ROLE {} {} PASSWORD {}").format(
                sql.SQL(action), role, sql.SQL(ROLE_FLAGS), sql.Literal(verifier)
            )
        )
        cur.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(database), role)
        )
        cur.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))
        # PostgreSQL 15+ already denies this; upgraded clusters may not.
        cur.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        table_rights = sql.SQL("SELECT, INSERT, UPDATE, DELETE")
        cur.execute(
            sql.SQL("GRANT {} ON ALL TABLES IN SCHEMA public TO {}").format(table_rights, role)
        )
        cur.execute(
            sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}").format(role)
        )
        # The migration role owns the schema version; the application only needs to read it.
        cur.execute(
            sql.SQL("REVOKE INSERT, UPDATE, DELETE ON alembic_version FROM {}").format(role)
        )
        # Tables and sequences added by later migrations (the owner runs them) follow.
        cur.execute(
            sql.SQL(
                "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public GRANT {} ON TABLES TO {}"
            ).format(sql.Identifier(owner), table_rights, role)
        )
        cur.execute(
            sql.SQL(
                "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public "
                "GRANT USAGE, SELECT ON SEQUENCES TO {}"
            ).format(sql.Identifier(owner), role)
        )
        if int(version) >= 170000:
            for table in ANALYZED_TABLES:
                cur.execute("SELECT to_regclass(%s) IS NOT NULL", (f"public.{table}",))
                found = cur.fetchone()
                if found is not None and found[0]:
                    cur.execute(
                        sql.SQL("GRANT MAINTAIN ON {} TO {}").format(sql.Identifier(table), role)
                    )
        cur.execute(
            f"SELECT {' OR '.join(PRIVILEGED_ATTRIBUTES)} FROM pg_roles WHERE rolname = %s",
            (user,),
        )
        privileged = cur.fetchone()
        if privileged is None or privileged[0]:
            raise ProvisionError(f"role {user} is still privileged; refusing to finish")
        cur.execute(BORROWED_PRIVILEGE, {"user": user, "owner": owner})
        borrowed = [row[0] for row in cur.fetchall()]
        if borrowed:
            # Nothing is revoked here (this script only adds); the operator decides.
            raise ProvisionError(
                f"role {user} is a member of {', '.join(borrowed)} and could SET ROLE to it; "
                f"revoke that membership (REVOKE <role> FROM {user};) and run this again"
            )
    conn.commit()


def main() -> int:
    user = os.environ.get("POSTGRES_APP_USER", "").strip()
    password = os.environ.get("POSTGRES_APP_PASSWORD", "")
    url = os.environ.get("DATABASE_URL", "").replace("+asyncpg", "").replace("+psycopg", "")
    if not url:
        print("provision-app-role: DATABASE_URL is required", file=sys.stderr)
        return 2
    owner = psycopg.conninfo.conninfo_to_dict(url).get("user", "")
    try:
        allow_legacy = os.environ.get("HEALTHOS_ALLOW_LEGACY_SECRETS", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if not requested_role(user, password, owner=str(owner), allow_legacy=allow_legacy):
            if os.environ.get("POSTGRES_ALLOW_OWNER_RUNTIME", "").strip() == "1":
                print("provision-app-role: owner runtime explicitly allowed; nothing to do")
                return 0
            with psycopg.connect(url, autocommit=True) as conn:
                if not holds_archive(conn) and not application_has_run(conn):
                    raise ProvisionError(
                        "this database holds no records, so it is a new install: set "
                        "POSTGRES_APP_USER and POSTGRES_APP_PASSWORD so the app does not run "
                        "as the database owner (POSTGRES_ALLOW_OWNER_RUNTIME=1 overrides this)"
                    )
            print(
                "provision-app-role: no application role configured; this existing install "
                "keeps connecting as the owner. In production the app refuses to start when "
                "that owner is a superuser (the postgres image makes it one): set "
                "POSTGRES_APP_USER and POSTGRES_APP_PASSWORD (docs/DEPLOYMENT.md)"
            )
            return 0
        with psycopg.connect(url, autocommit=False) as conn:
            provision(conn, user, password)
    except ProvisionError as error:
        print(f"provision-app-role: {error}", file=sys.stderr)
        return 2
    print(f"provision-app-role: role {user} ready (non-superuser)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
