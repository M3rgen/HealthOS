"""Production refuses a privileged database login; a dated, explicit opt-in bridges an upgrade.

A superuser can ``ALTER TABLE ... DISABLE TRIGGER`` and ``COPY ... PROGRAM``, so it undoes the
append-only guards the application role exists for. An install made before ``POSTGRES_APP_*``
still connects as the image's owner, which is a superuser: it gets a startup error that names the
exact step, or, with ``HEALTHOS_ALLOW_SUPERUSER_UNTIL``, at most ``GRACE_LIMIT`` more days.
"""

from __future__ import annotations

from datetime import date, timedelta

PRIVILEGED_ATTRIBUTES = (
    "rolsuper",
    "rolcreaterole",
    "rolcreatedb",
    "rolreplication",
    "rolbypassrls",
)
PRIVILEGED_BUILTIN_ROLES = (
    "pg_execute_server_program",
    "pg_read_server_files",
    "pg_write_server_files",
    "pg_database_owner",
)


def borrowed_privilege_sql(user_sql: str, owner_sql: str) -> str:
    """Build the shared membership policy from trusted SQL expressions, never user input."""
    attributes = " OR ".join(f"r.{name}" for name in PRIVILEGED_ATTRIBUTES)
    roles = ", ".join(f"'{name}'" for name in PRIVILEGED_BUILTIN_ROLES)
    return f"""
SELECT r.rolname FROM pg_roles r
WHERE r.rolname <> {user_sql}
  AND pg_has_role({user_sql}, r.oid, 'MEMBER')
  AND (r.rolname = {owner_sql}
       OR {attributes}
       OR r.rolname IN ({roles})
       OR r.oid IN (SELECT relowner FROM pg_class
                    WHERE relnamespace = 'public'::regnamespace))
ORDER BY r.rolname
"""


GRACE_SETTING = "HEALTHOS_ALLOW_SUPERUSER_UNTIL"
GRACE_LIMIT = timedelta(days=30)
MIGRATION_STEP = (
    "add POSTGRES_APP_USER=healthos_app and POSTGRES_APP_PASSWORD=<generated with: "
    'python3 -c "import secrets; print(secrets.token_hex(32))"> to .env and run '
    "`docker compose up -d`: the provision-app-role service creates the role and the app "
    "connects as it (docs/DEPLOYMENT.md, Application database role)"
)


PRIVILEGED_MIGRATION_STEP = (
    "remove the dangerous membership with `REVOKE <role> FROM <login>;` or attributes with "
    "`ALTER ROLE <login> NOCREATEROLE NOCREATEDB NOREPLICATION NOBYPASSRLS;`, "
    "then run `docker compose up -d` (docs/RUNBOOK.md, Application database role)"
)


class SuperuserRuntimeRefused(RuntimeError):
    """Production must not run as a privileged login; the message names the way out."""


def superuser_grace(until: date | None, today: date, *, privileged: bool = False) -> date:
    """The last day a privileged login is accepted, or SuperuserRuntimeRefused."""
    kind = "privileged" if privileged else "superuser"
    step = PRIVILEGED_MIGRATION_STEP if privileged else MIGRATION_STEP
    if until is None:
        raise SuperuserRuntimeRefused(
            f"production refuses a {kind} database login: "
            f"{step}. To keep running as is while you do, set {GRACE_SETTING} to a "
            f"date at most {GRACE_LIMIT.days} days ahead"
        )
    if until < today:
        raise SuperuserRuntimeRefused(
            f"{GRACE_SETTING}={until.isoformat()} has passed and production refuses a {kind} "
            f"database login: {step}"
        )
    if until > today + GRACE_LIMIT:
        raise SuperuserRuntimeRefused(
            f"{GRACE_SETTING} may be at most {GRACE_LIMIT.days} days ahead "
            f"({(today + GRACE_LIMIT).isoformat()} today); production refuses a {kind} "
            f"database login: {step}"
        )
    return until
