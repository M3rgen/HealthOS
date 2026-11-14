"""Latest Google, manual and sample-data body measurements for the dashboard profile."""

from datetime import datetime
from typing import NamedTuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.normalization import first_number
from app.core.providers import DEMO
from app.core.records import RecordType
from app.core.timeutil import as_utc
from app.models.health import Weight
from app.models.state import RawHealthData
from app.parsers.common import number, payload_for
from app.schemas.dashboard import ProfileMeasurement
from app.services.identities.source_precedence import label
from app.services.readers import DashboardSources
from app.services.readers.current_weight import latest_weight
from app.services.readers.dashboard_manual import latest_measurement_value
from app.services.shared.finite_math import finite_number, finite_scale


class _GoogleScalar(NamedTuple):
    """A profile metric read from one Google data type: its value fields and unit scale."""

    data_type: str
    metric: str
    fields: tuple[str, ...]
    scale: float


_GOOGLE_SCALARS = (
    _GoogleScalar("height", "height", ("heightMillimeters",), 0.1),
    _GoogleScalar("body-fat", "body_fat", ("percentage",), 1),
)


async def _google_measurements(
    session: AsyncSession, stop: datetime, sources: DashboardSources
) -> list[ProfileMeasurement]:
    measurements = []
    weight = await latest_weight(session, stop, sources.normalized(Weight, "weight"))
    if weight is not None:
        measured_at, kilograms = weight
        weight_item: ProfileMeasurement = {
            "metric": "weight",
            "value": kilograms,
            "measured_at": measured_at,
            "source": "Google",
        }
        if kilograms is None:
            weight_item["uncertainties"] = ["conflicting_values"]
        measurements.append(weight_item)
    for scalar in _GOOGLE_SCALARS:
        measurement = await _google_scalar(session, stop, sources, scalar)
        if measurement is not None:
            measurements.append(measurement)
    return measurements


async def _google_scalar(
    session: AsyncSession,
    stop: datetime,
    sources: DashboardSources,
    scalar: _GoogleScalar,
) -> ProfileMeasurement | None:
    """Read the selected source's entire newest group with constant accumulator space.

    Preserve the existing query's first-source selection, including an invalid latest
    group. Only finite nonnegative values can conflict; invalid values retain the
    existing absent/null distinction when no valid value is available. A supplied
    negative still establishes the latest group, so older measurements stay hidden.
    """
    data_type, metric, fields, scale = scalar
    candidates = await session.stream(
        select(RawHealthData.source_start_time, RawHealthData.raw_json)
        .where(
            RawHealthData.data_type == data_type,
            RawHealthData.source_start_time < stop,
            sources.raw((data_type,)),
        )
        .order_by(RawHealthData.source_start_time.desc())
        .execution_options(yield_per=100)
    )
    latest_at, source, value = None, None, None
    supplied_any = conflicting = False
    try:
        async for at, record in candidates:
            if latest_at is None:
                latest_at, source = as_utc(at), record.get("dataSource")
            elif as_utc(at) != latest_at:
                break
            if record.get("dataSource") != source:
                continue
            supplied = first_number(
                payload_for(RecordType(data_type), record), fields, parse=number
            )
            supplied_any |= supplied is not None
            if supplied is None:
                continue
            # Check the source Decimal before conversion can underflow its sign away.
            # Nonfinite values keep finite_scale's existing missing-value behavior.
            if supplied.is_finite() and supplied < 0:
                continue
            candidate = finite_scale(supplied, scale)
            if candidate is not None:
                if value is not None and candidate != value:
                    conflicting = True
                value = candidate
    finally:
        await candidates.close()
    # Unlike weight, an invalid latest height/fat group never revives an older value.
    if latest_at is not None and supplied_any:
        measurement: ProfileMeasurement = {
            "metric": metric,
            "value": None if conflicting else value,
            "measured_at": latest_at,
            "source": "Google",
        }
        if conflicting:
            measurement["uncertainties"] = ["conflicting_values"]
        return measurement
    return None


async def _manual_measurements(session: AsyncSession, stop: datetime) -> list[ProfileMeasurement]:
    measurements = []
    for metric in ("height", "weight", "body_fat", "muscle"):
        latest = await latest_measurement_value(session, metric, stop)
        if latest is None:
            continue
        measured_at, value = latest
        measurement: ProfileMeasurement = {
            "metric": metric,
            "value": finite_number(value),
            "measured_at": as_utc(measured_at),
            "source": "Manuel",
        }
        if value is None:
            measurement["uncertainties"] = ["conflicting_values"]
        measurements.append(measurement)
    return measurements


async def _sample_weight(session: AsyncSession, stop: datetime) -> list[ProfileMeasurement]:
    """The synthetic sample data's latest weight (``healthos demo``); it has no raw row, so
    the Google reader above never sees it."""
    item = await session.scalar(
        select(Weight)
        .where(Weight.provider == DEMO, Weight.timestamp < stop, Weight.is_quarantined.is_(False))
        .order_by(Weight.timestamp.desc())
        .limit(1)
    )
    if item is None:
        return []
    value = finite_number(item.kilograms)
    if value is None:
        return []
    return [
        {
            "metric": "weight",
            "value": value,
            "measured_at": as_utc(item.timestamp),
            "source": label(DEMO) or DEMO,
        }
    ]


async def dashboard_profile(
    session: AsyncSession, stop: datetime, sources: DashboardSources
) -> dict[str, ProfileMeasurement]:
    measurements = await _google_measurements(session, stop, sources)
    measurements.extend(await _sample_weight(session, stop))
    measurements.extend(await _manual_measurements(session, stop))
    profile = {}
    for item in measurements:
        current = profile.get(item["metric"])
        if current is None or item["measured_at"] >= current["measured_at"]:
            profile[item["metric"]] = item
    return profile
