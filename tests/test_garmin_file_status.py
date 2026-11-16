"""The existing health and Cihazlarım status readers agree on current file counts."""

from datetime import timedelta

from garmin_records import activity, store_page
from support.garmin_files import NOW, PREFIX, pull_service, seed_activities, zipped
from test_garmin_health import normalization_responses

from app.services.dashboard_views.devices import device_report


async def test_counts_reach_health_garmin_and_device_status(sessions, tmp_path, monkeypatch):
    await seed_activities(sessions, 4)
    pull, server, clock = pull_service(sessions, tmp_path)
    server.statuses[PREFIX + "2"] = [404]
    server.bodies[PREFIX + "3"] = zipped(b"not-a-fit-file" * 2)
    try:
        await pull.activity_files(3)
        expected = {"downloaded": 1, "pending": 1, "unavailable": 1, "invalid": 1}
        for correction in (False, True):
            if correction:
                clock.now += timedelta(hours=2)
                await store_page(
                    sessions,
                    "activities",
                    [
                        {**activity(1, NOW.date()), "revision": 1},
                    ],
                    clock(),
                )
                expected = {"downloaded": 0, "pending": 2, "unavailable": 1, "invalid": 1}
            health, status, garmin = await normalization_responses(
                pull.settings, sessions, monkeypatch
            )
            assert health["garmin"]["activity_files"] == expected
            assert status["garmin"]["activity_files"] == garmin["activity_files"] == expected
            async with sessions() as session:
                report = await device_report(session, now=clock())
                device = next(d for d in report["devices"] if d["id"] == "garmin")
                assert device["activity_files"] == expected
    finally:
        await pull.aclose()
