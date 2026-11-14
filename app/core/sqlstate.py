"""Validated driver error codes, independent of the database layer."""

import re

LOCK_NOT_AVAILABLE = "55P03"
DEADLOCK_DETECTED = "40P01"
SERIALIZATION_FAILURE = "40001"
# statement_timeout, a client cancel request or pg_cancel_backend() ended the statement.
QUERY_CANCELED = "57014"
_SQLSTATE = re.compile(r"[0-9A-Z]{5}")


def sqlstate(exc: object) -> str | None:
    """Read a SQLAlchemy-wrapped or native driver code, never exception text or SQL.

    asyncpg exposes ``sqlstate`` and older drivers expose ``pgcode``. Validate both
    before returning a code so callers can safely include it in value-free diagnostics.
    """
    original = getattr(exc, "orig", exc)
    for attribute in ("sqlstate", "pgcode"):
        code = getattr(original, attribute, None)
        if isinstance(code, str) and _SQLSTATE.fullmatch(code):
            return code
    return None


# Module consumers can distinguish extraction from native driver attribute access.
extract = sqlstate
