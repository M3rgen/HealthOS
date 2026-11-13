"""Latest Google, manual and sample-data body measurements for the dashboard profile."""

from datetime import datetime

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
from app.services.readers.dashboard_manual import latest_measurements
from app.services.shared.finite_math import finite_number, finite_scale


async def _google_measurements(
    session: AsyncSession, stop: datetime, sources: DashboardSources
) -> list[ProfileMeasurement]:
    measurements = []
    weight = await latest_weight(session, stop, sources.normalized(Weight, "weight"))
    if weight is not None:
        measured_at, kilograms = weight
        measurements.append(
            {
                "metric": "weight",
                "value": kilograms,
                "measured_at": measured_at,
                "source": "Google",
            }
        )
    for data_type, metric, fields, scale in (
        ("height", "height", ("heightMillimeters",), 0.1),
        ("body-fat", "body_fat", ("percentage",), 1),
    ):
        item = await session.scalar(
            select(RawHealthData)
            .where(
                RawHealthData.data_type == data_type,
                RawHealthData.source_start_time < stop,
                sources.raw((data_type,)),
            )
            .order_by(RawHealthData.source_start_time.desc())
            .limit(1)
        )
        if item:
            supplied = first_number(
                payload_for(RecordType(data_type), item.raw_json), fields, parse=number
            )
            if supplied is not None:
                measurements.append(
                    {
                        "metric": metric,
                        "value": finite_scale(supplied, scale),
                        "measured_at": as_utc(item.source_start_time),
                        "source": "Google",
                    }
                )
    return measurements


async def _manual_measurements(session: AsyncSession, stop: datetime) -> list[ProfileMeasurement]:
    measurements = []
    for metric in ("height", "weight", "body_fat", "muscle"):
        rows = (await session.scalars(latest_measurements(metric, stop))).all()
        if not rows:
            continue
        item = rows[0]
        conflicting = len({row.value for row in rows}) > 1
        measurement: ProfileMeasurement = {
            "metric": metric,
            "value": None if conflicting else finite_number(item.value),
            "measured_at": as_utc(item.measured_at),
            "source": "Manuel",
        }
        if conflicting:
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
