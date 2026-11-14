"""Google archive platforms compete by minute without changing stored evidence."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import fit_factory
import pytest
from derived_records import snapshot, store
from test_heart_corrections import heart
from test_heart_corrections import heart_batch as heart_batch

from app.services.dashboard_views.dashboard import dashboard_data
from app.services.identities.source_precedence import Policy, label
from app.services.providers.fit_import import import_fit_bytes
from app.services.providers.ingestion_readers import ARCHIVES, google_heart
from app.services.readers.dashboard_queries import DashboardQuery
from app.services.readers.reading import ArchiveReaders

START = fit_factory.START


async def unhinted(session, start, stop, source_filter):
    return replace(await google_heart(session, start, stop, source_filter), resolve_minutes=False)


@pytest.mark.parametrize("archives", [ARCHIVES, (ArchiveReaders("mixed", heart_rate=unhinted),)])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("timezone,hour", [("UTC", 10), ("Asia/Kathmandu", 15)])
@pytest.mark.parametrize(
    "samples,means,minutes",
    [
        (
            [(0, "FITBIT", 60), (30, "HEALTH_KIT", 100)],
            (60, 100),
            ({"FITBIT": 1}, {"HEALTH_KIT": 1}),
        ),
        (
            [
                (0, "FITBIT", 60),
                (20, "FITBIT", 66),
                (30, "HEALTH_KIT", 100),
                (60, "HEALTH_KIT", 90),
            ],
            (72, 95),
            ({"FITBIT": 1, "HEALTH_KIT": 1}, {"HEALTH_KIT": 2}),
        ),
        (
            [(0, "FITBIT", 60), (60, "HEALTH_KIT", 100)],
            (80, 80),
            ({"FITBIT": 1, "HEALTH_KIT": 1},) * 2,
        ),
        ([(0, "FITBIT", 60), (20, "FITBIT", 61), (60, "FITBIT", 81)], (67.3, 67.3), (None, None)),
        ([], (None, None), (None, None)),
        (
            [(0, "FITBIT", -60), (30, "HEALTH_KIT", 100)],
            (100, 100),
            (None, None),
        ),
        ([(0, "FITBIT", "-1e-1000")], (None, None), (None, None)),
    ],
)
async def test_google_platform_chart(
    sessions, archives, reverse, timezone, hour, samples, means, minutes
):
    await store(
        sessions,
        "heart-rate",
        *[
            heart(
                str(index),
                START + timedelta(seconds=seconds),
                {"platform": code},
                beatsPerMinute=bpm,
            )
            for index, (seconds, code, bpm) in enumerate(samples)
        ],
    )
    order = ("HEALTH_KIT", "FITBIT") if reverse else ("FITBIT", "HEALTH_KIT")
    policy = Policy(health=order)
    async with sessions() as session:
        before = await snapshot(session)
        result = await dashboard_data(
            session,
            archives=archives,
            policy=policy,
            query=DashboardQuery(START.date(), 1, timezone),
        )
        assert await snapshot(session) == before
    expected = [None] * 24
    expected[hour] = means[reverse]
    assert result["charts"]["heart_rate"] == expected
    codes = sorted(
        {code for _, code, bpm in samples if Decimal(str(bpm)) >= 0},
        key=lambda code: policy.rank(code, "health"),
    )
    source = result["charts"]["sources"]["heart_rate"]
    assert source["source"] == (codes[0] if codes else None)
    assert source["label"] == label(codes[0] if codes else None)
    assert source["alternatives"] == [{"source": code, "label": label(code)} for code in codes[1:]]
    assert source["activity_windows"] is None
    assert source.get("minutes_by_source") == minutes[reverse]


@pytest.mark.parametrize("manufacturer,provider", [(1, "GARMIN"), (255, "FIT")])
@pytest.mark.parametrize("reverse", [False, True])
async def test_google_platforms_alongside_fit_archive(sessions, manufacturer, provider, reverse):
    async with sessions.begin() as session:
        await import_fit_bytes(session, fit_factory.activity(manufacturer=manufacturer, bpm=120))
    await store(
        sessions,
        "heart-rate",
        *[
            heart(code, START, {"platform": code}, beatsPerMinute=bpm)
            for code, bpm in (("FITBIT", 60), ("HEALTH_KIT", 100))
        ],
    )
    policy = Policy(activity=("HEALTH_KIT", "GARMIN", "FITBIT")) if reverse else Policy()
    async with sessions() as session:
        before = await snapshot(session)
        result = await dashboard_data(
            session, policy=policy, query=DashboardQuery(START.date(), 1, "UTC")
        )
        assert await snapshot(session) == before
    assert result["charts"]["heart_rate"][10] == (
        100 if reverse else 120.5 if provider == "GARMIN" else 60
    )
    source = result["charts"]["sources"]["heart_rate"]
    assert source["source"] == "FITBIT"
    assert {item["source"] for item in source["alternatives"]} == {provider, "HEALTH_KIT"}
    assert source["activity_windows"] == provider
