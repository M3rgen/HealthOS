"""Adapt provider archive reads to the shared capabilities used by projections.

Reading an archive never constructs an HTTP client or requires credentials.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.core.providers import APPLE, DEMO, FIT
from app.services.identities.source_precedence import GARMIN, google_source_of
from app.services.interpretation.google_daily_readers import archive_bounds as google_bounds
from app.services.interpretation.google_daily_readers import daily_values as google_days
from app.services.readers import ArchiveReaders, DailyRead, DashboardSources, DashboardWindow
from app.services.readers.current_heart import chart_heart_rate_samples as heart_rate_samples
from app.services.readers.current_workouts import current_workouts
from app.services.readers.reading import BoundsReader, DailyValue, HeartRead, WorkoutRecord

from . import apple_readers
from .garmin_readers import activity_windows, daily_values, workouts
from .garmin_readers import archive_bounds as garmin_bounds
from .garmin_readers import heart_rate_samples as provider_heart


async def google_heart(
    session: AsyncSession, start: datetime, stop: datetime, source_filter: ColumnElement[bool]
) -> HeartRead:
    """Keep each platform's samples intact for the projection's minute selection.

    Google is one archive, but it can deliver several competing source platforms.
    A genuinely single-platform read retains its historical hourly aggregation.
    """
    samples = [
        (moment, bpm, google_source_of((platform,)))
        for moment, bpm, platform in await heart_rate_samples(session, start, stop, source_filter)
    ]
    platforms = {source for _, _, source in samples}
    return HeartRead(
        samples,
        google_source_of(platforms) if samples else None,
        resolve_minutes=len(platforms) > 1,
    )


def provider_archive(
    name: str, provider: str, bounds: BoundsReader | None = None, *, daily: bool = True
) -> ArchiveReaders:
    """The readers of a provider whose rows carry the Garmin shape (``garmin_readers``):
    Garmin, other FIT manufacturers, and synthetic sample data (``DEMO``), each with its
    own source code so precedence and every label keep them apart. Activity-only FIT
    imports have no daily totals, sleep sessions or resting heart rate to read."""

    async def provider_workouts(
        session: AsyncSession, start: datetime, stop: datetime
    ) -> list[WorkoutRecord]:
        return await workouts(session, start, stop, provider)

    async def samples(
        session: AsyncSession, start: datetime, stop: datetime, source_filter: ColumnElement[bool]
    ) -> HeartRead:
        found = await provider_heart(session, start, stop, provider)
        windows = await activity_windows(session, start, stop, provider) if found else []
        return HeartRead(
            [(moment, bpm, provider) for moment, bpm in found],
            provider if found else None,
            bool(found),
            windows,
            provider if windows else None,
        )

    async def days(
        session: AsyncSession,
        window: DashboardWindow,
        end_date: date,
        zone: ZoneInfo,
        sources: DashboardSources,
    ) -> tuple[DailyRead, ...]:
        values = await daily_values(
            session, window.first, end_date, zone, metrics=sources.metrics, provider=provider
        )
        return (
            DailyRead(
                {
                    day: {
                        metric: DailyValue(
                            value,
                            provider,
                            provider,
                            ("from_normalized_columns",) if metric == "sleep_seconds" else (),
                        )
                        for metric, value in metrics.items()
                    }
                    for day, metrics in values.items()
                }
            ),
        )

    return ArchiveReaders(name, samples, provider_workouts, days if daily else None, bounds)


async def apple_samples(
    session: AsyncSession, start: datetime, stop: datetime, source_filter: ColumnElement[bool]
) -> HeartRead:
    """Apple's samples under ``HEALTH_KIT``, the code of the Apple data Google relays: the
    dashboard keeps Google's sample of a minute both carry (``_heart_chart``)."""
    samples = await apple_readers.heart_rate_samples(session, start, stop)
    return HeartRead(
        [(moment, bpm, apple_readers.HEALTH_KIT) for moment, bpm in samples],
        apple_readers.HEALTH_KIT if samples else None,
        bool(samples),
    )


async def apple_days(
    session: AsyncSession,
    window: DashboardWindow,
    end_date: date,
    zone: ZoneInfo,
    sources: DashboardSources,
) -> tuple[DailyRead, ...]:
    """Apple's day values under ``HEALTH_KIT`` with provider ``APPLE``: on a day Google relays
    the same Apple data, Google's value comes first and Apple's stays its alternative."""
    days = await apple_readers.daily_values(
        session, window.first, end_date, zone, metrics=sources.metrics
    )
    return (
        DailyRead(
            {
                day: {
                    metric: DailyValue(
                        value,
                        apple_readers.HEALTH_KIT,
                        APPLE,
                        ("from_normalized_columns",) if metric == "sleep_seconds" else (),
                    )
                    for metric, value in values.items()
                }
                for day, values in days.items()
            }
        ),
    )


GOOGLE_ARCHIVE = ArchiveReaders(
    "google", google_heart, current_workouts, google_days, google_bounds
)
GARMIN_ARCHIVE = provider_archive("garmin", GARMIN, garmin_bounds)
FIT_ARCHIVE = provider_archive("fit", FIT, daily=False)
# The sample archive pages no history beyond its own window: no bounds reader.
DEMO_ARCHIVE = provider_archive("demo", DEMO)
# GitHub-only Apple extension: after Google's, so a minute or day both carry keeps Google's.
APPLE_ARCHIVE = ArchiveReaders(
    "apple", apple_samples, apple_readers.workouts, apple_days, apple_readers.archive_bounds
)
ARCHIVES = (GOOGLE_ARCHIVE, GARMIN_ARCHIVE, FIT_ARCHIVE, DEMO_ARCHIVE, APPLE_ARCHIVE)
