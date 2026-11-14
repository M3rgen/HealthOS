"""A third archive participates in production reads through typed capabilities alone."""

# pyright: strict

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import assert_type
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement
from test_heart_corrections import heart as heart_record
from test_heart_corrections import heart_batch as heart_batch

from app.db.contracts import SessionFactory
from app.google_health.data_types import REGISTRY
from app.services.archive.storage import store_records
from app.services.dashboard_views.dashboard import dashboard_data
from app.services.dashboard_views.dashboard_history import history_data
from app.services.dashboard_views.dashboard_workouts import list_workouts
from app.services.identities.source_precedence import Policy
from app.services.providers.ingestion_readers import ARCHIVES, google_heart
from app.services.readers.dashboard_queries import DashboardQuery
from app.services.readers.dashboard_sources import DashboardSources
from app.services.readers.dashboard_window import DashboardWindow, daily_window
from app.services.readers.reading import (
    ArchiveBounds,
    ArchiveReaders,
    DailyRead,
    DailyValue,
    HeartRead,
    HistoryMetric,
    WorkoutRecord,
)

DAY = date(2026, 11, 6)
START = datetime(2026, 11, 6, 10, tzinfo=UTC)
STOP = START + timedelta(hours=1)


@pytest.mark.parametrize(
    "platforms,codes",
    [
        ([], []),
        (["FITBIT", "FITBIT"], ["FITBIT", "FITBIT"]),
        ([" fitbit ", "FITBIT"], ["FITBIT", "FITBIT"]),
        ([None, ""], ["GOOGLE", "GOOGLE"]),
        (["FITBIT", "HEALTH_KIT"], ["FITBIT", "HEALTH_KIT"]),
    ],
)
async def test_google_heart_preserves_samples_and_reports_competing_platforms(
    sessions: SessionFactory, platforms: list[str | None], codes: list[str]
) -> None:
    async with sessions.begin() as session:
        await store_records(
            session,
            REGISTRY["heart-rate"],
            [
                heart_record(str(index), START, {"platform": platform}, beatsPerMinute=60 + index)
                for index, platform in enumerate(platforms)
            ],
        )
    async with sessions() as session:
        read = await google_heart(session, START, STOP, true())
    assert read.samples == [(START, 60 + index, code) for index, code in enumerate(codes)]
    assert read.resolve_minutes == (len(set(codes)) > 1)
    assert read.summary_source == (
        codes[0] if len(set(codes)) == 1 else "GOOGLE" if codes else None
    )
    assert read.activity_windows == [] and read.activity_source is None


class SyntheticArchive:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def bounds(
        self, session: AsyncSession, metric: HistoryMetric, today: date, zone: ZoneInfo
    ) -> ArchiveBounds:
        assert today == DAY and zone.key == "UTC"
        self.calls.append(f"bounds:{metric}")
        return ArchiveBounds(DAY - timedelta(days=65), DAY)

    async def daily(
        self,
        session: AsyncSession,
        window: DashboardWindow,
        end_date: date,
        zone: ZoneInfo,
        sources: DashboardSources,
    ) -> tuple[DailyRead, ...]:
        self.calls.append("daily")
        assert window.first == end_date == DAY and zone.key == "UTC"
        return (
            DailyRead(
                {
                    DAY: {
                        "steps": DailyValue(456, "SYNTHETIC", "SYNTHETIC"),
                        "sleep_seconds": DailyValue(3600, "SYNTHETIC", "SYNTHETIC", ("synthetic",)),
                    }
                },
                [456.0] + [None] * 23,
            ),
        )

    async def heart_rate(
        self,
        session: AsyncSession,
        start: datetime,
        stop: datetime,
        source_filter: ColumnElement[bool],
    ) -> HeartRead:
        self.calls.append("heart")
        assert start <= START < stop
        return HeartRead(
            [(START, 71.0, "SYNTHETIC")], "SYNTHETIC", True, [(START, STOP)], "SYNTHETIC"
        )

    async def workouts(
        self, session: AsyncSession, start: datetime, stop: datetime
    ) -> list[WorkoutRecord]:
        self.calls.append("workouts")
        assert start <= START < stop
        return [
            {
                "id": "synthetic-workout",
                "exercise_type": "RUNNING",
                "start_time": START,
                "end_time": STOP,
                "duration_seconds": 3600.0,
                "calories_kcal": 0.0,
                "distance_meters": None,
                "source_code": "SYNTHETIC",
                "provider": "SYNTHETIC",
            }
        ]


@pytest.mark.parametrize("alongside_existing", [False, True])
async def test_third_provider_drives_dashboard_and_workouts(
    sessions: SessionFactory, alongside_existing: bool
) -> None:
    fake = SyntheticArchive()
    archive = ArchiveReaders("synthetic", fake.heart_rate, fake.workouts, fake.daily)
    archives = (*ARCHIVES, archive) if alongside_existing else (archive,)
    # An archive can advertise no capabilities without a consumer knowing its name.
    archives = (*archives, ArchiveReaders("unsupported"))
    policy = Policy(activity=("SYNTHETIC", "GARMIN"), health=("SYNTHETIC", "FITBIT"))
    if alongside_existing:
        async with sessions.begin() as session:
            for kind, payload in (
                (
                    "steps",
                    {
                        "count": 123,
                        "interval": {
                            "startTime": START.isoformat(),
                            "endTime": STOP.isoformat(),
                        },
                    },
                ),
                (
                    "heart-rate",
                    {"sampleTime": {"physicalTime": START.isoformat()}, "beatsPerMinute": 99},
                ),
            ):
                await store_records(
                    session,
                    REGISTRY[kind],
                    [
                        {
                            "name": f"synthetic/third-provider/{kind}",
                            "dataSource": {"platform": "FITBIT"},
                            REGISTRY[kind].json_key: payload,
                        }
                    ],
                )
    async with sessions() as session:
        data = await dashboard_data(
            session,
            archives=archives,
            policy=policy,
            query=DashboardQuery(DAY, 1, "UTC"),
        )
        rows = await list_workouts(session, DAY, ZoneInfo("UTC"), archives=archives)
    assert data["days"][0]["steps"] == 456
    assert data["days"][0]["sleep_assumptions"] == ["synthetic"]
    assert data["days"][0]["sources"]["steps"]["source"] == "SYNTHETIC"
    assert data["charts"]["heart_rate"][10] == 71.0
    assert data["charts"]["sources"]["heart_rate"]["activity_windows"] == "SYNTHETIC"
    assert data["charts"]["sources"]["heart_rate"]["minutes_by_source"] == {"SYNTHETIC": 1}
    assert rows[0]["id"] == "synthetic-workout" and rows[0]["provider"] == "SYNTHETIC"
    assert rows[0]["duration_seconds"] == 3600 and rows[0]["calories_kcal"] == 0
    assert fake.calls == ["daily", "heart", "workouts"]
    if alongside_existing:
        assert data["days"][0]["sources"]["steps"]["alternatives"] == [
            {"source": "FITBIT", "label": "Fitbit", "value": 123}
        ]
        assert data["charts"]["sources"]["heart_rate"]["alternatives"] == [
            {"source": "FITBIT", "label": "Fitbit"}
        ]
    else:
        assert data["charts"]["steps"] == [456.0] + [None] * 23


async def test_absent_read_capabilities_are_empty(sessions: SessionFactory) -> None:
    async with sessions() as session:
        data = await dashboard_data(
            session,
            archives=(ArchiveReaders("empty"),),
            query=DashboardQuery(DAY, 1, "UTC"),
        )
        assert data["days"][0]["status"] == "empty"
        assert data["charts"]["heart_rate"] == [None] * 24
        assert await list_workouts(session, DAY, ZoneInfo("UTC"), archives=()) == []
        history = await history_data(
            session, "steps", 1, "UTC", DAY, archives=(ArchiveReaders("empty"),)
        )
        assert history["earliest_date"] is history["latest_date"] is None
        assert history["total_pages"] == 1
        assert all(row["value"] is None for row in history["days"])


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize(
    "bounds,means,counts",
    [((30, 90), (106.7, 100.4), (2, 2)), ((20, 50), (84.7, 122.4), (2, 1))],
)
async def test_heart_chart_averages_original_samples_across_exact_workout_boundaries(
    sessions: SessionFactory,
    reverse: bool,
    bounds: tuple[int, int],
    means: tuple[float, float],
    counts: tuple[int, int],
) -> None:
    windows = [(START + timedelta(seconds=bounds[0]), START + timedelta(seconds=bounds[1]))]

    def archive(source: str, values: list[float]) -> ArchiveReaders:
        async def heart(
            session: AsyncSession,
            start: datetime,
            stop: datetime,
            source_filter: ColumnElement[bool],
        ) -> HeartRead:
            samples = [
                (START + timedelta(seconds=offset), bpm, source)
                for offset, bpm in zip((15, 30, 45, 59, 75, 90, 105), values, strict=True)
            ]
            return HeartRead(samples, source, True, windows if source == "GARMIN" else [], "GARMIN")

        return ArchiveReaders(source, heart_rate=heart)

    policy = (
        Policy(activity=("FITBIT", "GARMIN"), health=("GARMIN", "FITBIT")) if reverse else Policy()
    )
    async with sessions() as session:
        data = await dashboard_data(
            session,
            archives=(
                archive("FITBIT", [60, 61, 66, 67, 69, 72, 75]),
                archive("GARMIN", [100, 120, 130, 140, 150, 160, 180]),
            ),
            policy=policy,
            query=DashboardQuery(DAY, 1, "UTC"),
        )
    assert data["charts"]["heart_rate"] == [None] * 10 + [means[reverse]] + [None] * 13
    health, activity = policy.health[0], policy.activity[0]
    assert data["charts"]["sources"]["heart_rate"]["minutes_by_source"] == {
        health: counts[0],
        activity: counts[1],
    }


async def test_reader_signatures_keep_result_types(sessions: SessionFactory) -> None:
    fake = SyntheticArchive()
    readers = ArchiveReaders("typed", fake.heart_rate, fake.workouts, fake.daily, fake.bounds)
    assert readers.heart_rate is not None and readers.workouts is not None
    assert readers.daily is not None and readers.archive_bounds is not None
    async with sessions() as session:
        assert_type(
            await readers.archive_bounds(session, "steps", DAY, ZoneInfo("UTC")), ArchiveBounds
        )
        assert_type(await readers.workouts(session, START, STOP), list[WorkoutRecord])
        assert_type(await readers.heart_rate(session, START, STOP, true()), HeartRead)
        assert_type(
            await readers.daily(
                session,
                daily_window(DAY, 1, ZoneInfo("UTC")),
                DAY,
                ZoneInfo("UTC"),
                DashboardSources(),
            ),
            Sequence[DailyRead],
        )


@pytest.mark.parametrize(
    "metric,key", [("steps", "steps"), ("sleep", "sleep_seconds"), ("heart", "resting_bpm")]
)
@pytest.mark.parametrize("alongside_existing", [False, True])
async def test_history_uses_third_archive_bounds_and_skips_absent_capabilities(
    sessions: SessionFactory, metric: HistoryMetric, key: str, alongside_existing: bool
) -> None:
    fake = SyntheticArchive()
    oldest = DAY - timedelta(days=65)

    async def daily(
        session: AsyncSession,
        window: DashboardWindow,
        end_date: date,
        zone: ZoneInfo,
        sources: DashboardSources,
    ) -> tuple[DailyRead, ...]:
        assert sources.metrics == (key,)
        assert window.first <= oldest <= end_date and zone.key == "UTC"
        fake.calls.append("daily")
        return (DailyRead({oldest: {key: DailyValue(123, "SYNTHETIC", "SYNTHETIC")}}),)

    supported = ArchiveReaders("unknown", daily=daily, archive_bounds=fake.bounds)
    archives = (*ARCHIVES, supported) if alongside_existing else (supported,)
    archives = (*archives, ArchiveReaders("no-boundary-capability"))
    async with sessions() as session:
        history = await history_data(session, metric, 3, "UTC", DAY, archives=archives)
    assert history["total_pages"] == 3
    assert history["earliest_date"] == oldest and history["latest_date"] == DAY
    assert next(row for row in history["days"] if row["date"] == oldest.isoformat())["value"] == 123
    assert fake.calls == [f"bounds:{metric}", "daily"]


@pytest.mark.parametrize("version", ["v1", "v2", "mixed"])
async def test_garmin_archive_rebuild_removes_stale_legacy_members_and_preserves_quarantine(
    sessions: SessionFactory, version: str
) -> None:
    from garmin_records import store_page
    from sqlalchemy import func, select, update

    from app.models.garmin import GarminHeartSet
    from app.models.health import HeartRate
    from app.services.providers.garmin_normalize import build_rows, normalize_window, write_rows
    from app.services.providers.garmin_readers import archive_summary
    from app.services.providers.ingestion_readers import GARMIN_ARCHIVE

    garmin_samples = GARMIN_ARCHIVE.heart_rate
    assert garmin_samples is not None

    ms = int(START.timestamp() * 1000)
    a = {"calendarDate": str(DAY), "heartRateValues": [[ms, 61], [ms + 60_000, 64]]}
    b = {**a, "heartRateValues": a["heartRateValues"][:1]}
    # Simulate an archive normalized before membership tracking existed.
    async with sessions.begin() as session:
        rows, _ = build_rows({"heart-rate": {str(DAY): a}}, DAY, DAY, "Garmin")
        for index, row in enumerate(rows[HeartRate]):
            if version == "v1" or (version == "mixed" and index == 1):
                row["google_data_point_id"] = row["google_data_point_id"].removesuffix(":v2")
        await write_rows(session, HeartRate, rows[HeartRate])
    await store_page(sessions, "heart-rate", [a], STOP)
    await store_page(sessions, "heart-rate", [b], STOP + timedelta(minutes=1))
    async with sessions() as session:
        assert len((await garmin_samples(session, START, STOP, true())).samples) == 2
        before = (await session.execute(select(HeartRate.__table__))).all()
    # Re-normalize existing evidence; no network or credentials are needed.
    async with sessions.begin() as session:
        await normalize_window(session, DAY, DAY)
    async with sessions() as session:
        read = await garmin_samples(session, START, STOP, true())
        assert read.samples == [(START, 61, "GARMIN")]
        assert read.summary_source == "GARMIN" and read.resolve_minutes
        summary = await archive_summary(session)
        assert summary is not None and summary["last_data"] == START
        kept = (
            await session.execute(
                select(HeartRate.__table__).where(HeartRate.id.in_([row.id for row in before]))
            )
        ).all()
        assert {row.id: row for row in kept} == {row.id: row for row in before}
    # Exclude t2 while it is absent: a return to A must not undo that decision.
    async with sessions.begin() as session:
        await session.execute(
            update(HeartRate)
            .where(HeartRate.timestamp == START + timedelta(minutes=1))
            .values(is_quarantined=True, quarantine_reason="Synthetic exclusion")
        )
    await store_page(sessions, "heart-rate", [a], STOP + timedelta(minutes=2))
    async with sessions.begin() as session:
        await normalize_window(session, DAY, DAY)
    async with sessions() as session:
        assert (await garmin_samples(session, START, STOP, true())).samples == [
            (START, 61, "GARMIN")
        ]
        excluded = (
            await session.scalars(
                select(HeartRate).where(HeartRate.timestamp == START + timedelta(minutes=1))
            )
        ).all()
        assert all(
            row.is_quarantined and row.quarantine_reason == "Synthetic exclusion"
            for row in excluded
        )
        assert await session.scalar(select(func.count()).select_from(GarminHeartSet)) == 2


async def test_empty_daily_set_preserves_google_and_fit_readings(sessions: SessionFactory) -> None:
    from fit_factory import START as FIT_START
    from fit_factory import activity
    from garmin_records import store_page
    from identity_records import HEART, heart

    from app.services.providers.fit_import import import_fit
    from app.services.providers.fit_records import parse_fit
    from app.services.providers.garmin_normalize import normalize_window
    from app.services.providers.ingestion_readers import GARMIN_ARCHIVE, google_heart

    garmin_samples = GARMIN_ARCHIVE.heart_rate
    assert garmin_samples is not None

    day = FIT_START.date()
    stop = FIT_START + timedelta(hours=1)
    async with sessions.begin() as session:
        await import_fit(session, parse_fit(activity()))
        await store_records(session, HEART, [heart(80, at=FIT_START)])
    ms = int(FIT_START.timestamp() * 1000)
    for index, samples in enumerate(([[ms, 61]], [])):
        await store_page(
            sessions,
            "heart-rate",
            [{"calendarDate": str(day), "heartRateValues": samples}],
            stop + timedelta(minutes=index),
        )
        async with sessions.begin() as session:
            await normalize_window(session, day, day)
        async with sessions() as session:
            garmin = await garmin_samples(session, FIT_START, stop, true())
            assert sorted(bpm for _, bpm, _ in garmin.samples) == (
                [61, 120, 121] if index == 0 else [120, 121]
            )
            google = await google_heart(session, FIT_START, stop, true())
            assert google.samples == [(FIT_START, 80, "FITBIT")]


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("preferred", ["FITBIT", "GARMIN"])
@pytest.mark.parametrize("count", [456, 0])
@pytest.mark.parametrize("age", [0, 1])
async def test_step_chart_tracks_normalized_daily_selection_without_writes(
    sessions: SessionFactory, reverse: bool, preferred: str, count: int, age: int
) -> None:
    from derived_records import steps
    from garmin_records import daily_steps, store_page
    from sqlalchemy import select

    from app.db.base import Base
    from app.services.providers.garmin_normalize import normalize_window

    day = DAY - timedelta(days=age)
    start = START - timedelta(days=age)
    async with sessions.begin() as session:
        await store_records(
            session,
            REGISTRY["steps"],
            [steps(count=123, start=start), steps("zero", 0, start + timedelta(hours=1))],
        )
    await store_page(sessions, "steps", [daily_steps(day, steps=count)], STOP)
    async with sessions.begin() as session:
        await normalize_window(session, day, day)
    policy = Policy(health=(preferred, "GARMIN" if preferred == "FITBIT" else "FITBIT"))
    archives = tuple(reversed(ARCHIVES)) if reverse else ARCHIVES
    async with sessions() as session:
        before = [
            (await session.execute(select(table).order_by(*table.primary_key))).all()
            for table in Base.metadata.tables.values()
        ]
        data = await dashboard_data(
            session, archives=archives, policy=policy, query=DashboardQuery(DAY, 2, "UTC")
        )
        assert not session.new and not session.dirty and not session.deleted
        assert before == [
            (await session.execute(select(table).order_by(*table.primary_key))).all()
            for table in Base.metadata.tables.values()
        ]
    assert data["days"][age]["steps"] == (123 if preferred == "FITBIT" else count)
    assert data["days"][age]["sources"]["steps"]["source"] == preferred
    source = None if age else preferred
    assert data["charts"]["sources"]["steps"]["source"] == source
    assert data["charts"]["sources"]["steps"]["label"] == (
        None if age else "Fitbit" if preferred == "FITBIT" else "Garmin"
    )
    assert data["charts"]["steps"] == (
        [None] * 10 + [123.0, 0.0] + [None] * 12
        if not age and preferred == "FITBIT"
        else [None] * 24
    )
    if age:
        assert data["days"][0]["steps"] is None


@pytest.mark.parametrize(
    "first_value,second_value,first_hours,expected",
    [
        (None, 456, [123.0] + [None] * 23, [456.0] + [None] * 23),
        (None, 0, [123.0] + [None] * 23, [0.0] + [None] * 23),
        (456, 456, None, [None] * 24),
        (None, None, [123.0] + [None] * 23, [None] * 24),
    ],
)
async def test_step_hours_belong_to_observation_even_when_source_codes_match(
    sessions: SessionFactory,
    first_value: float | None,
    second_value: float | None,
    first_hours: list[float | None] | None,
    expected: list[float | None],
) -> None:
    def archive(provider: str, value: float | None, hours: list[float | None] | None):
        async def daily(
            session: AsyncSession,
            window: DashboardWindow,
            end_date: date,
            zone: ZoneInfo,
            sources: DashboardSources,
        ) -> tuple[DailyRead, ...]:
            return (DailyRead({DAY: {"steps": DailyValue(value, "SAME", provider)}}, hours),)

        return ArchiveReaders(provider, daily=daily)

    async with sessions() as session:
        data = await dashboard_data(
            session,
            archives=(
                archive("FIRST", first_value, first_hours),
                archive("SECOND", second_value, [second_value] + [None] * 23),
            ),
            query=DashboardQuery(DAY, 1, "UTC"),
        )
    assert data["charts"]["steps"] == expected
    assert data["days"][0]["steps"] == (first_value if first_value is not None else second_value)
