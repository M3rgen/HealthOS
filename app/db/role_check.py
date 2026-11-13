"""Whether the runtime database login has more privilege than the application needs."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.role_policy import PRIVILEGED_ATTRIBUTES, borrowed_privilege_sql
from app.db.dialect import database_operation

SUPERUSER = "superuser"
PRIVILEGED_ROLE = "privileged_role"
SCHEMA_OWNER = "schema_owner"

_BORROWED_PRIVILEGE = borrowed_privilege_sql(
    "current_user", "pg_catalog.pg_get_userbyid(c.relowner)"
)
_ROLE_QUERY = text(
    f"""
    SELECT r.rolsuper,
           {" OR ".join(f"r.{name}" for name in PRIVILEGED_ATTRIBUTES if name != "rolsuper")},
           EXISTS ({_BORROWED_PRIVILEGE}),
           COALESCE(pg_catalog.pg_get_userbyid(c.relowner) = current_user, false)
    FROM pg_catalog.pg_roles r
    LEFT JOIN pg_catalog.pg_class c ON c.oid = to_regclass('public.raw_health_data')
    WHERE r.rolname = current_user
    """
)


@database_operation
async def database_role_risk(session: AsyncSession) -> str | None:
    """The highest runtime role risk, or None; only PostgreSQL has the distinction."""
    row = (await session.execute(_ROLE_QUERY)).one_or_none()
    if row is None:
        return None
    if row[0]:
        return SUPERUSER
    if row[1] or row[2]:
        return PRIVILEGED_ROLE
    return SCHEMA_OWNER if row[3] else None
