"""Manual measurement persistence and append-only flag decisions for the dashboard."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.manual import ManualMeasurement
from app.schemas.dashboard import Measurement, MeasurementFlag
from app.services.archive.manual_flags import active_flags, excluded_measurements, record_flag

from .manual_validation import validate_measurement

MEASUREMENT_LIST_LIMIT = 100


class MeasurementNotFound(LookupError):
    """No manual measurement has the requested id."""


def latest_measurements(metric: str, stop: datetime) -> Select[tuple[ManualMeasurement]]:
    """Eligible rows at the latest measurement time strictly before ``stop``.

    Entry time never makes a backdated measurement newer. Test flags are excluded before
    finding that time. Both the profile and canonical adapter keep ties as evidence:
    different values at that time are a conflict, not an entry-time tiebreaker.
    """
    valid = (
        ManualMeasurement.metric == metric,
        ManualMeasurement.measured_at < stop,
        ManualMeasurement.id.not_in(excluded_measurements()),
    )
    latest = select(func.max(ManualMeasurement.measured_at)).where(*valid).scalar_subquery()
    return (
        select(ManualMeasurement)
        .where(*valid, ManualMeasurement.measured_at == latest)
        .order_by(ManualMeasurement.created_at.desc())
    )


async def latest_measurement_value(
    session: AsyncSession, metric: str, stop: datetime
) -> tuple[datetime, Decimal | None] | None:
    """Summarize every eligible row at the latest time in constant result space.

    Reuse the canonical query's eligibility and newest-group selection, but replace
    its evidence projection with SQL aggregates. Empty groups have no timestamp;
    unequal stored Numeric extrema signal a conflict before any float conversion.
    The value column is non-nullable, so a missing value denotes only a conflict.
    No row limit or entry-time tiebreaker may hide a different value in the group.
    """
    statement = latest_measurements(metric, stop).with_only_columns(
        func.max(ManualMeasurement.measured_at),
        func.min(ManualMeasurement.value),
        func.max(ManualMeasurement.value),
    )
    measured_at, minimum, maximum = (await session.execute(statement.order_by(None))).one()
    if measured_at is None:
        return None
    return measured_at, minimum if minimum == maximum else None


async def create_measurement(session: AsyncSession, values: dict[str, Any]) -> Measurement:
    validate_measurement(values)
    item = ManualMeasurement(**values)
    session.add(item)
    await session.flush()
    await session.refresh(item)
    return {
        "id": str(item.id),
        "metric": item.metric,
        "value": item.value,
        "measured_at": item.measured_at,
        "source": "Manuel",
    }


async def list_measurements(session: AsyncSession) -> list[Measurement]:
    rows = (
        await session.scalars(
            select(ManualMeasurement)
            .order_by(ManualMeasurement.measured_at.desc())
            .limit(MEASUREMENT_LIST_LIMIT)
        )
    ).all()
    flags = await active_flags(session, [row.id for row in rows])
    return [
        {
            "id": str(row.id),
            "metric": row.metric,
            "value": row.value,
            "measured_at": row.measured_at,
            "source": "Manuel",
            # Flags set on it now; a "test" entry is listed but left out of the profile.
            "flags": flags.get(row.id, []),
        }
        for row in rows
    ]


async def set_measurement_flag(
    session: AsyncSession,
    measurement_id: uuid.UUID,
    flag: str,
    flagged: bool,
    reason: str | None,
) -> MeasurementFlag:
    if await session.get(ManualMeasurement, measurement_id) is None:
        raise MeasurementNotFound("Ölçüm bulunamadı.")
    changed = await record_flag(session, measurement_id, flag, flagged, reason)
    await session.flush()
    flags = await active_flags(session, [measurement_id])
    return {"id": str(measurement_id), "flags": flags.get(measurement_id, []), "changed": changed}
