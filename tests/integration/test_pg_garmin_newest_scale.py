"""Cihazlarım's newest Garmin heart time walks back from the newest rows; history is not read.

The recent rows hold the answer and, newer than it, rows each filter must reject: Google
samples, a quarantined sample, a sample outside its day's latest membership set and a FIT
sample whose raw record was deleted at the source. Older history grows fivefold.
"""

from datetime import UTC, datetime, timedelta

import pytest
from garmin_projection_archive import analyze, explain
from sqlalchemy import func, select, text

from app.models.health import HeartRate
from app.services.providers.garmin_readers import newest_current, newest_since
from app.services.providers.garmin_versions import current_row

pytestmark = pytest.mark.integration
NOW = datetime(2026, 1, 10, 12, tzinfo=UTC)
ANSWER = NOW - timedelta(hours=3)
HISTORY_END = NOW - timedelta(days=2)
SAMPLES = """
INSERT INTO heart_rate (id, google_data_point_id, provider, source_record_id, timestamp, bpm,
                        is_quarantined)
SELECT gen_random_uuid(),
       CAST(:prefix AS text) || extract(epoch FROM t)::bigint || CAST(:suffix AS text), :provider,
       :source, t, 70, :quarantined
FROM generate_series(CAST(:start AS timestamptz), CAST(:stop AS timestamptz),
                     CAST(:step AS interval)) t
"""


async def add(
    session, provider, prefix, start, stop, *, step=timedelta(minutes=1), source=None, **flags
):
    await session.execute(
        text(SAMPLES),
        {
            "prefix": prefix,
            "suffix": flags.get("suffix", ""),
            "provider": provider,
            "source": source,
            "start": start,
            "stop": stop,
            "step": step,
            "quarantined": flags.get("quarantined", False),
        },
    )


async def recent_rows(session) -> None:
    await add(session, "GOOGLE", "g:", HISTORY_END, NOW)
    await add(session, "GARMIN", "garmin:hr:", ANSWER, ANSWER, source="heart-rate:a", suffix=":v2")
    await add(session, "GARMIN", "garmin:hr:", ANSWER, ANSWER, source="heart-rate:a")  # v1 twin
    await add(session, "GARMIN", "garmin:q:", NOW - timedelta(hours=2), NOW, quarantined=True)
    # Day "b" has a newer membership set that does not list these samples.
    await add(session, "GARMIN", "garmin:b:", NOW - timedelta(hours=1), NOW, source="heart-rate:b")
    await add(session, "GARMIN", "fit:hr:", NOW - timedelta(minutes=30), NOW, source="fit:1")
    await session.execute(
        text(
            "INSERT INTO garmin_raw_records (id, kind, key, body, body_sha256, first_seen_at) "
            "VALUES (1, 'heart-rate', 'b', '{}', repeat('0', 64), :now);"
        ),
        {"now": NOW},
    )
    await session.execute(
        text(
            "INSERT INTO garmin_heart_sets (id, source_record_id, raw_record_id, members_sha256) "
            "VALUES (1, 'heart-rate:b', 1, repeat('0', 64))"
        )
    )
    await session.execute(
        text(
            "INSERT INTO raw_health_data (id, google_data_point_id, data_type, raw_json, "
            "payload_hash, first_seen_at, last_seen_at, source_deleted_at) "
            "SELECT gen_random_uuid(), google_data_point_id, 'garmin-fit-heart-rate', '{}', 'x', "
            ":now, :now, :now FROM heart_rate WHERE source_record_id = 'fit:1'"
        ),
        {"now": NOW},
    )


async def test_newest_heart_time_reads_only_rows_newer_than_the_answer(pg_sessions):
    plans, days = {}, 0
    async with pg_sessions.begin() as session:
        await recent_rows(session)
    for grown in (6, 30):
        async with pg_sessions.begin() as session:
            start, stop = HISTORY_END - timedelta(days=grown), HISTORY_END - timedelta(days=days)
            last = stop - timedelta(minutes=2)
            await add(session, "GOOGLE", "g:", start, last)
            await add(
                session,
                "GARMIN",
                "garmin:o:",
                start,
                last,
                step=timedelta(minutes=2),
                source="heart-rate:o",
            )
        days = grown
        await analyze(pg_sessions.kw["bind"])
        async with pg_sessions() as session:
            assert await newest_current(session, HeartRate, HeartRate.timestamp, NOW) == ANSWER
            whole = select(func.max(HeartRate.timestamp)).where(
                HeartRate.provider == "GARMIN",
                current_row(HeartRate),
                HeartRate.is_quarantined.is_(False),
            )
            assert await session.scalar(whole) == ANSWER
            statement = newest_since(HeartRate, HeartRate.timestamp, NOW - timedelta(days=30))
            plans[grown] = await explain(await session.connection(), statement)
    print(plans)
    assert plans[30]["rows"] == 1
    assert plans[30]["work"] <= plans[6]["work"] + 10, plans
