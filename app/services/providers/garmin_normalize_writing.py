"""Idempotent Garmin projection writes; raw records and older versions stay intact."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import Table, bindparam, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase

from app.core.providers import GARMIN
from app.core.timeutil import as_utc
from app.db.dialect import dialect_insert

from .garmin_versions import VERSION

# Never compare bookkeeping or exclusion columns as measurement values.
_SKIP_COMPARE = {"id", "created_at", "updated_at", "is_quarantined", "quarantine_reason"}


def _comparable(value: Any) -> Any:
    if isinstance(value, datetime):
        return as_utc(value)
    if isinstance(value, Decimal):
        return value.normalize()
    return value


def unique_heart_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the first prepared sample for each normalized heart key, in source order.

    Call only after numeric column preparation: an invalid or overflowing candidate
    must not reserve a key and hide a later usable sample. Deduplicate before sorting
    for writes, comparing existing values, counting rows or publishing membership.
    Both projection writes and membership must consume this same selected list.
    """
    selected: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = row["google_data_point_id"]
        # A later conflicting value must never replace the first prepared choice.
        if key in selected:
            continue
        selected[key] = row
    return list(selected.values())


async def write_rows(
    session: AsyncSession, model: type[DeclarativeBase], rows: list[dict[str, Any]]
) -> tuple[int, int, int]:
    """Insert the new rows, update the changed ones, leave the rest alone.

    Rows must already be prepared and unique by normalized key (``build_rows``).
    Returns (inserted, updated, unchanged). Rows are matched by ``google_data_point_id``
    (and ``data_type`` for scalar metrics). Only ``provider = 'GARMIN'`` rows are written
    onto: a Google row under the same key (impossible by construction) would be left as is.
    """
    if not rows:
        return 0, 0, 0
    table = cast(Table, model.__table__)
    keys = sorted(
        {row["google_data_point_id"] for row in rows}
        | {row["google_data_point_id"].removesuffix(f":{VERSION}") for row in rows}
    )
    columns = [column.name for column in table.columns if column.name not in _SKIP_COMPARE]
    existing = await _existing_rows(session, table, keys, columns)
    inserts: list[dict[str, Any]] = []
    # Rows that changed the same columns are one parameterized statement, not one each.
    changes: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    inserted = updated = unchanged = 0
    now = datetime.now(UTC)
    for row in rows:
        current = existing.get(row["google_data_point_id"])
        if current is None:
            legacy = existing.get(row["google_data_point_id"].removesuffix(f":{VERSION}"))
            if legacy is not None and legacy["provider"] == GARMIN:
                # Versioning must not revive an excluded measurement.
                row = {
                    **row,
                    **{name: legacy[name] for name in ("is_quarantined", "quarantine_reason")},
                }
            row = {"is_quarantined": False, "quarantine_reason": None, **row}
            inserts.append(row)
            continue
        if current["provider"] != GARMIN:
            unchanged += 1
            continue
        changed = {
            name: value
            for name, value in row.items()
            if name in columns and _comparable(current[name]) != _comparable(value)
        }
        if not changed:
            unchanged += 1
            continue
        names = tuple(sorted(changed))
        changes.setdefault(names, []).append(
            {"row_id": current["id"], **{f"new_{name}": changed[name] for name in names}}
        )
        updated += 1
    for names, params in changes.items():
        statement = (
            update(table)
            .where(table.c.id == bindparam("row_id"))
            .values(
                {name: bindparam(f"new_{name}", type_=table.c[name].type) for name in names}
                | {"updated_at": now}
            )
            .execution_options(synchronize_session=False)
        )
        for offset in range(0, len(params), 1000):
            await session.execute(statement, params[offset : offset + 1000])
    if inserts:
        conflict = ["google_data_point_id"]
        if "data_type" in table.c:
            conflict.insert(0, "data_type")
        statement = dialect_insert(session, table).on_conflict_do_nothing(index_elements=conflict)
        for offset in range(0, len(inserts), 1000):
            await session.execute(statement, inserts[offset : offset + 1000])
        inserted = len(inserts)
    return inserted, updated, unchanged


async def _existing_rows(
    session: AsyncSession, table: Table, keys: list[str], columns: list[str]
) -> dict[str, dict[str, Any]]:
    existing: dict[str, dict[str, Any]] = {}
    for offset in range(0, len(keys), 1000):
        found = await session.execute(
            select(
                *(table.c[name] for name in ("id", "is_quarantined", "quarantine_reason", *columns))
            ).where(table.c.google_data_point_id.in_(keys[offset : offset + 1000]))
        )
        for row in found.mappings():
            existing[row["google_data_point_id"]] = dict(row)
    return existing
