import struct
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import fit_factory
import pytest
from derived_records import snapshot
from garmin_records import activity, heart_rate, store_page
from identity_records import HEART, heart, store
from sqlalchemy import select, true, update

from app.core.config import Settings
from app.core.fit import FIT_EXERCISE
from app.models.health import ExerciseSession
from app.models.state import RawHealthData
from app.services.dashboard_views.dashboard import dashboard_data
from app.services.dashboard_views.dashboard_workouts import list_workouts
from app.services.identities import source_precedence
from app.services.providers.fit_import import import_fit_bytes
from app.services.providers.garmin_normalize import normalize_window
from app.services.providers.ingestion_readers import FIT_ARCHIVE, GARMIN_ARCHIVE
from app.services.readers.dashboard_queries import DashboardQuery
from app.services.umay.codex_health import read_health

START = datetime(2026, 11, 1, tzinfo=UTC)
STOP, BEGIN, END = START + timedelta(days=1), START - timedelta(days=2), START + timedelta(hours=12)


@pytest.fixture(params=["api", "garmin-fit", "fit"])
async def archive(sessions, monkeypatch, request):
    origin = request.param
    payload = activity(1, BEGIN.date())
    payload.update(startTimeGMT=BEGIN.isoformat(), duration=(END - BEGIN).total_seconds())
    if origin == "api":
        await store_page(sessions, "activities", [payload], STOP)
    else:
        stamp = int((BEGIN - fit_factory.EPOCH).total_seconds())
        monkeypatch.setattr(fit_factory, "STAMP", stamp)
        messages = fit_factory.activity_messages(
            manufacturer=255 if origin == "fit" else 1, elapsed=int(payload["duration"] * 1000)
        )
        for hour in (1, 18):
            moment = int((START + timedelta(hours=hour) - fit_factory.EPOCH).total_seconds())
            messages += b"\x02" + struct.pack("<IB", moment, 120)
        async with sessions.begin() as session:
            await import_fit_bytes(session, fit_factory.fit_file(messages))
    daily = heart_rate(START.date())
    daily["heartRateValues"] = [
        [int((START + timedelta(hours=hour)).timestamp() * 1000), 120] for hour in (1, 18)
    ]
    await store_page(sessions, "heart-rate", [daily], STOP)
    async with sessions.begin() as session:
        await normalize_window(session, BEGIN.date(), START.date())
        if origin == "api":
            values = dict(
                (await session.execute(select(ExerciseSession.__table__))).mappings().one()
            )
            values.pop("id")
            values["google_data_point_id"] = values["google_data_point_id"].removesuffix(":v2")
            session.add(ExerciseSession(**{**values, "end_time": END + timedelta(hours=1)}))
    for hour in (1, 18):
        await store(sessions, HEART, heart(100, START + timedelta(hours=hour)))
    return origin, FIT_ARCHIVE if origin == "fit" else GARMIN_ARCHIVE, payload


@pytest.mark.parametrize("reverse", [False, True])
async def test_long_session_consumers(sessions, monkeypatch, archive, reverse):
    _, reader, _ = archive
    activity, health = (
        ("FITBIT,GARMIN", "GARMIN,FITBIT") if reverse else ("GARMIN,FITBIT", "FITBIT,GARMIN")
    )
    settings = Settings(
        _env_file=None, source_precedence_activity=activity, source_precedence_health=health
    )
    monkeypatch.setattr(source_precedence, "get_settings", lambda: settings)
    async with sessions() as session:
        before = await snapshot(session)
        samples = await reader.heart_rate(session, START, STOP, true())
        assert samples.activity_windows == [(BEGIN, END)]
        assert samples.activity_source == ("FIT" if archive[0] == "fit" else "GARMIN")
        assert await list_workouts(session, START.date(), ZoneInfo("UTC")) == []
        previous = await list_workouts(session, BEGIN.date().replace(day=1), ZoneInfo("UTC"))
        assert [row["start_time"] for row in previous] == [BEGIN]
        dashboard = await dashboard_data(session, query=DashboardQuery(START.date(), 1, "UTC"))
    query = dict(action="daily", start_date="2026-11-01", end_date="2026-11-01", timezone="UTC")
    umay = await read_health(sessions, query)
    expected = [None] * 24
    expected[1], expected[18] = (100, 120) if reverse else (120, 100)
    assert umay["charts"]["heart_rate"] == dashboard["charts"]["heart_rate"] == expected
    sources = dashboard["charts"]["sources"]["heart_rate"]
    assert umay["charts"]["sources"]["heart_rate"] == sources
    assert sources["activity_windows"] == samples.activity_source
    async with sessions() as session:
        assert await snapshot(session) == before


@pytest.mark.parametrize(
    "begin,end,quarantine",
    [
        (BEGIN, START, False),
        (STOP, STOP + timedelta(hours=1), False),
        (BEGIN, BEGIN, False),
        (BEGIN, BEGIN - timedelta(seconds=1), False),
        (BEGIN, END, True),
    ],
)
async def test_excluded_sessions(sessions, archive, begin, end, quarantine):
    _, reader, _ = archive
    async with sessions.begin() as session:
        changes = dict(start_time=begin, end_time=end, is_quarantined=quarantine)
        await session.execute(update(ExerciseSession).values(**changes))
    async with sessions() as session:
        assert (await reader.heart_rate(session, START, STOP, true())).activity_windows == []
        result = await dashboard_data(session, query=DashboardQuery(START.date(), 1, "UTC"))
        assert result["charts"]["heart_rate"][1] == 100


@pytest.mark.parametrize("mark", ["is_quarantined", "source_deleted_at"])
async def test_raw_ineligibility_does_not_reopen_retained_sessions(sessions, archive, mark):
    origin, reader, payload = archive
    if origin == "api":
        await store_page(
            sessions, "activities", [{**payload, "duration": 0}], STOP + timedelta(minutes=1)
        )
        async with sessions.begin() as session:
            await normalize_window(session, BEGIN.date(), START.date())
    else:
        async with sessions.begin() as session:
            await session.execute(
                update(RawHealthData)
                .where(RawHealthData.data_type == FIT_EXERCISE.name)
                .values(**{mark: True if mark == "is_quarantined" else STOP})
            )
    async with sessions() as session:
        assert (await reader.heart_rate(session, START, STOP, true())).activity_windows == []
