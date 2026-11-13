"""Dashboard, history, CSV and Umay share committed daily eligibility."""

import csv
import io
from datetime import timedelta

import pytest
from garmin_records import store_page
from test_garmin_daily_projections import CASES, corrected
from test_garmin_normalize import DAY, NOW

from app.api.dashboard import export
from app.services.dashboard_views.dashboard import dashboard_data
from app.services.dashboard_views.dashboard_history import history_data
from app.services.providers.garmin_normalize import normalize_window
from app.services.readers.dashboard_queries import DashboardQuery
from app.services.umay.codex_health import read_health


@pytest.mark.parametrize("kind,model,body,path,metric", [c for c in CASES if c[-1]])
async def test_daily_consumers_agree_after_correction(sessions, kind, model, body, path, metric):
    a = {**body, "calendarDate": str(DAY)}
    for index, payload in enumerate((a, corrected(a, path, "null"), a)):
        await store_page(sessions, kind, [payload], NOW + timedelta(minutes=index))
        async with sessions.begin() as session:
            await normalize_window(session, DAY, DAY)
        async with sessions() as session:
            dashboard = await dashboard_data(session, query=DashboardQuery(DAY, 1, "UTC"))
            exported = await export(days=1, end_date=DAY, timezone="UTC", session=session)
            if metric in ("steps", "resting_bpm", "sleep_seconds"):
                history = await history_data(
                    session,
                    {"steps": "steps", "resting_bpm": "heart", "sleep_seconds": "sleep"}[metric],
                    1,
                    "UTC",
                    DAY,
                )
                assert history["days"][0]["value"] == dashboard["days"][0][metric]
                assert history["earliest_date"] == (None if index == 1 else DAY)
                assert history["latest_date"] == (None if index == 1 else DAY)
        umay = await read_health(
            sessions,
            {"action": "daily", "start_date": str(DAY), "end_date": str(DAY), "timezone": "UTC"},
        )
        expected = dashboard["days"][0][metric]
        assert (expected is None) == (index == 1)
        assert umay["days"][0][metric] == expected
        csv_day = next(csv.DictReader(io.StringIO(exported.body.decode().lstrip("\ufeff"))))
        assert csv_day[metric] == ("" if expected is None else str(expected))


async def test_empty_missing_and_failed_pulls_do_not_invalidate_observed_values(sessions, tmp_path):
    from zoneinfo import ZoneInfo

    from garmin_records import FakeGarmin, daily_steps
    from sqlalchemy import select
    from test_garmin_pull import STEPS_4_DAYS, TODAY, Clock, service

    from app.models.garmin import GarminDailyProjection
    from app.services.providers.garmin_readers import daily_values

    key = tmp_path / "key"
    key.write_text("synthetic")
    fake = FakeGarmin()
    pull = service(sessions, fake, key, Clock())
    try:
        fake.bodies[STEPS_4_DAYS] = [daily_steps(TODAY)]
        assert not (await pull.run(["steps"])).failed
        async with sessions() as session:
            evidence = (await session.execute(select(GarminDailyProjection.__table__))).all()
        for status in (503, 200, 204):
            fake.bodies[STEPS_4_DAYS] = []
            fake.statuses[STEPS_4_DAYS] = [status] * 10 if status == 503 else [status]
            result = await pull.run(["steps"])
            assert result.failed == (status == 503)
            async with sessions() as session:
                assert (
                    await session.execute(select(GarminDailyProjection.__table__))
                ).all() == evidence
                assert (await daily_values(session, TODAY, TODAY, ZoneInfo("UTC")))[TODAY][
                    "steps"
                ] == 7000
        fake.bodies[STEPS_4_DAYS] = [{"calendarDate": str(TODAY), "totalSteps": None}]
        assert not (await pull.run(["steps"])).failed
        async with sessions() as session:
            assert await daily_values(session, TODAY, TODAY, ZoneInfo("UTC")) == {}
    finally:
        await pull.aclose()
