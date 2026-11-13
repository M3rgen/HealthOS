"""Bounded, ordered reads of append-only interpretation input state."""

import asyncio
from collections import defaultdict
from collections.abc import Sequence

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement

from app.db.dialect import database
from app.models.identity_tables import RawSourceDevice
from app.models.manual import ManualMeasurement
from app.models.manual_flags import ManualMeasurementFlagEvent
from app.services.identities.identity_common import among


async def input_rows[T](session: AsyncSession, statement: Select[tuple[T]]) -> list[T]:
    """Hydrate bounded ORM batches on the session thread, yielding between batches."""
    result = await session.stream_scalars(statement.execution_options(yield_per=256))
    rows = []
    try:
        async for batch in result.partitions(256):
            rows.extend(batch)
            await asyncio.sleep(0)
    finally:
        await result.close()
    return rows


async def source_decisions(
    session: AsyncSession, keys: Sequence[tuple[str, str]]
) -> list[RawSourceDevice]:
    """Read the latest decision for each exact (data type, fingerprint) pair.

    Grouping by data type keeps pairs intact while allowing `among` to use a
    PostgreSQL array. The database adapter bounds expanded parameter lists in tests.
    Sorted groups and chunks reproduce the original global key order, including
    missing keys. All reads use the caller's session and transaction.
    """
    groups: dict[str, list[str]] = defaultdict(list)
    for kind, fingerprint in sorted(set(keys)):
        groups[kind].append(fingerprint)
    decisions = []
    later = aliased(RawSourceDevice)
    latest = (
        ~select(later.id)
        .where(
            later.data_type == RawSourceDevice.data_type,
            later.fingerprint == RawSourceDevice.fingerprint,
            later.id > RawSourceDevice.id,
        )
        .exists()
    )
    for kind, fingerprints in groups.items():
        for block in database(session).value_blocks(fingerprints):
            decisions.extend(
                await input_rows(
                    session,
                    select(RawSourceDevice)
                    .where(
                        RawSourceDevice.data_type == kind,
                        among(
                            session,
                            RawSourceDevice.fingerprint,
                            block,
                        ),
                        latest,
                    )
                    .order_by(RawSourceDevice.data_type, RawSourceDevice.fingerprint),
                )
            )
    return decisions


async def measurement_flags(
    session: AsyncSession, measurement_window: Sequence[ColumnElement[bool]], record_limit: int
):
    """Read event history with one global order and one overflow sentinel.

    The caller has already checked that every measurement in this window fits
    its input budget. Selecting those same ids in SQL needs no per-id parameters,
    works for an empty window, and preserves marked measurements in the history.
    Do not filter to effective flags: mark/unmark must also change the fingerprint.
    """
    return (
        await session.execute(
            select(ManualMeasurementFlagEvent.id, ManualMeasurementFlagEvent.action)
            .where(
                ManualMeasurementFlagEvent.measurement_id.in_(
                    select(ManualMeasurement.id).where(*measurement_window)
                )
            )
            .order_by(ManualMeasurementFlagEvent.id)
            .limit(record_limit + 1)
        )
    ).all()
