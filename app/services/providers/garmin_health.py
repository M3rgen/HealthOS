"""Bounded, read-only health signals from Garmin's persisted pull checkpoints."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.timeutil import as_utc
from app.models.garmin import GarminPullState
from app.schemas.status import GarminNormalization

from .garmin_kinds import KIND_NAMES
from .garmin_normalization import ERROR_CODE, read_status


@dataclass(frozen=True)
class GarminHealthSnapshot:
    last_success_at: datetime | None
    last_error_at: datetime | None
    lag_seconds: int | None
    stale_after_seconds: int
    reasons: tuple[str, ...]
    normalization: GarminNormalization


async def read_health(
    session: AsyncSession, settings: Settings, now: datetime
) -> GarminHealthSnapshot:
    # Never read last_error: older checkpoints contain free text (including file paths).
    rows = (
        await session.execute(
            select(
                GarminPullState.last_success_at,
                GarminPullState.last_error_at,
            ).where(GarminPullState.kind.in_(KIND_NAMES))
        )
    ).all()
    times = [(as_utc(success), as_utc(error)) for success, error in rows]
    successes = [success for success, _ in times if success is not None]
    errors = [error for _, error in times if error is not None]
    # Two pull intervals plus the two quiet hours (02:00-03:59 UTC). A quiet night or
    # the default two-hour cadence must not trip Google's shorter freshness threshold.
    stale_after = 60 * max(
        settings.pipeline_stale_threshold_minutes, 2 * settings.garmin_pull_interval_minutes + 120
    )
    # The oldest kind determines lag: one fresh endpoint cannot hide a stalled one.
    lag = max(0, int((now - min(successes)).total_seconds())) if successes else None
    reasons = []
    if any(error is not None and (success is None or error > success) for success, error in times):
        reasons.append("garmin_pull_failed")
    if len(successes) < len(KIND_NAMES):
        reasons.append("garmin_pull_uninitialized")
    if lag is not None and lag >= stale_after:
        reasons.append("garmin_pull_stale")
    normalization = await read_status(session)
    if normalization["status"] == "degraded":
        reasons.append(ERROR_CODE)
    return GarminHealthSnapshot(
        last_success_at=max(successes, default=None),
        last_error_at=max(errors, default=None),
        lag_seconds=lag,
        stale_after_seconds=stale_after,
        reasons=tuple(reasons),
        normalization=normalization,
    )
