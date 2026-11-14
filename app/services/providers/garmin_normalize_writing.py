"""Idempotent Garmin projection writes; raw records and older versions stay intact."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import Select, Table, bindparam, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase

from app.core.providers import GARMIN
from app.core.timeutil import as_utc
from app.db.dialect import dialect_insert

from .garmin_versions import VERSION

# Never compare bookkeeping or exclusion columns as measurement values.
_SKIP_COMPARE = {"id", "created_at", "updated_at", "is_quarantined", "quarantine_reason"}


def _key_columns(table: Table) -> tuple[str, ...]:
    """The projection's existing conflict target, also used for reads and matching."""
    if "data_type" in table.c:
        return "data_type", "google_data_point_id"
    return ("google_data_point_id",)


def _row_key(table: Table, row: Any, *, legacy: bool = False) -> tuple[str, ...]:
    """Use the same unique key for lookup, matching and exclusion inheritance."""
    key = tuple(row[name] for name in _key_columns(table))
    if legacy:
        return (*key[:-1], key[-1].removesuffix(f":{VERSION}"))
    return key


def _lookup_keys(table: Table, rows: list[dict[str, Any]]) -> list[tuple[str, ...]]:
    # Include unversioned rows so exclusions survive projection version changes.
    return sorted(
        {_row_key(table, row) for row in rows} | {_row_key(table, row, legacy=True) for row in rows}
    )


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
    keys = _lookup_keys(table, rows)
    columns = [column.name for column in table.columns if column.name not in _SKIP_COMPARE]
    existing = await _existing_rows(session, table, keys, columns)
    inserts: list[dict[str, Any]] = []
    # Rows that changed the same columns are one parameterized statement, not one each.
    changes: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    inserted = updated = unchanged = 0
    now = datetime.now(UTC)
    for row in rows:
        current = existing.get(_row_key(table, row))
        if current is None:
            legacy = existing.get(_row_key(table, row, legacy=True))
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
        statement = dialect_insert(session, table).on_conflict_do_nothing(
            index_elements=list(_key_columns(table))
        )
        for offset in range(0, len(inserts), 1000):
            await session.execute(statement, inserts[offset : offset + 1000])
        inserted = len(inserts)
    return inserted, updated, unchanged


def _existing_query(table: Table, keys: list[tuple[str, ...]], columns: list[str]) -> Select:
    # The scalar unique index starts with data_type. ID-only lookups scan the
    # whole table and can match a different metric with the same ID.
    key_columns = [table.c[name] for name in _key_columns(table)]
    predicate = (
        tuple_(*key_columns).in_(keys)
        if len(key_columns) > 1
        else key_columns[0].in_([key[0] for key in keys])
    )
    return select(
        *(table.c[name] for name in ("id", "is_quarantined", "quarantine_reason", *columns))
    ).where(predicate)


async def _existing_rows(
    session: AsyncSession, table: Table, keys: list[tuple[str, ...]], columns: list[str]
) -> dict[tuple[str, ...], dict[str, Any]]:
    existing: dict[tuple[str, ...], dict[str, Any]] = {}
    for offset in range(0, len(keys), 1000):
        found = await session.execute(_existing_query(table, keys[offset : offset + 1000], columns))
        for row in found.mappings():
            existing[_row_key(table, row)] = dict(row)
    return existing
