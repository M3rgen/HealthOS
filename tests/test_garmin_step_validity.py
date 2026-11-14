"""Source signs, committed inputs and legacy step evidence stay read-only."""

import hashlib
from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest
from garmin_records import store_page
from identity_records import STEPS, fitbit, steps, store
from sqlalchemy import DateTime, bindparam, event, select, text, update
from support.postgres_reads import only_reads
from test_garmin_normalize import DAY, NOW

from app.models.garmin import (
    GarminApiResponse,
    GarminDailyProjection,
    GarminRawRecord,
    GarminRecordObservation,
)
from app.models.health import Steps
from app.services.dashboard_views.dashboard import dashboard_data
from app.services.identities.source_precedence import Policy
from app.services.providers.garmin_normalize import build_rows, normalize_window, write_rows
from app.services.providers.garmin_readers import archive_bounds, daily_values
from app.services.readers.dashboard_queries import DashboardQuery

ZONE = ZoneInfo("UTC")
TABLES = (GarminApiResponse, GarminRawRecord, GarminRecordObservation, GarminDailyProjection, Steps)


async def insert_raw_json(session, kind, body):
    # Bypass ORM JSON encoding: these are numeric tokens, not numeric strings.
    return await session.scalar(
        text(
            "INSERT INTO garmin_raw_records (kind, key, body, body_sha256, first_seen_at) "
            "VALUES (:kind, :key, :body, :digest, :seen) RETURNING id"
        ).bindparams(bindparam("seen", type_=DateTime(timezone=True))),
        {
            "kind": kind,
            "key": str(DAY),
            "body": body,
            "digest": hashlib.sha256(body.encode()).hexdigest(),
            "seen": NOW,
        },
    )


@pytest.mark.parametrize("projected", [False, True])
@pytest.mark.parametrize("normalized", [0, 7000])
@pytest.mark.parametrize("token", ["-1e-1000", '"-1e-1000"', "1e-1000", "0", "-0e-1000"])
async def test_json_tokens_keep_step_sign_before_driver_decoding(
    sessions, projected, normalized, token
):
    rows, _ = build_rows({"steps": {str(DAY): {"totalSteps": normalized}}}, DAY, DAY, "Test")
    values = rows[Steps][0]
    async with sessions.begin() as session:
        await insert_raw_json(session, "steps", '{"totalSteps": 7001}')  # Older valid input.
        raw_id = await insert_raw_json(session, "steps", '{"totalSteps": ' + token + "}")
        await write_rows(session, Steps, [values])
        if projected:
            session.add(
                GarminDailyProjection(
                    source_record_id=values["source_record_id"],
                    raw_record_ids={"steps": raw_id},
                    normalized_key=values["google_data_point_id"],
                )
            )
    expected = None if "-1e" in token else normalized
    await assert_read(sessions, expected)
    if projected:
        await store_page(sessions, "steps", [{"calendarDate": str(DAY), "totalSteps": 80}], NOW)
        await assert_read(sessions, expected)  # Pending inputs cannot replace publication.


async def snapshot(session):
    return {
        model: (await session.execute(select(model.__table__).order_by(model.id))).all()
        for model in TABLES
    }


def only_select(conn, cursor, statement, parameters, context, executemany):
    only_reads(conn, cursor, statement, parameters, context, executemany)


async def assert_read(sessions, expected):
    engine = sessions.kw["bind"].sync_engine
    async with sessions() as session:
        before = await snapshot(session)
        event.listen(engine, "before_cursor_execute", only_select)
        try:
            for _ in range(2):
                days = await daily_values(session, DAY, DAY, ZONE, metrics=("steps",))
                assert days == ({} if expected is None else {DAY: {"steps": expected}})
                bounds = await archive_bounds(session, "steps", DAY, ZONE)
                assert bounds.earliest == bounds.latest == (None if expected is None else DAY)
        finally:
            event.remove(engine, "before_cursor_execute", only_select)
        assert await snapshot(session) == before


@pytest.mark.parametrize(
    "value,expected",
    [
        (-1, None),
        (-0.0001, None),
        ("-1e-10000", None),
        (0, 0),
        (-0.0, 0),
        ("-0e-10000", 0),
        (12.25, 12.25),
        ("12.25", 12.25),
        ("1e-10000", 0),
    ],
)
async def test_committed_source_sign_survives_rounding_pending_inputs_and_reversions(
    sessions, value, expected
):
    previous = None
    for index, source in enumerate((7000, value, value, 7000)):
        await store_page(
            sessions,
            "steps",
            [{"calendarDate": str(DAY), "totalSteps": source}],
            NOW + timedelta(minutes=index),
        )
        # A new raw version cannot change the last committed projection's sign.
        await assert_read(sessions, previous)
        async with sessions.begin() as session:
            await normalize_window(session, DAY, DAY)
        previous = expected if index in (1, 2) else 7000
        await assert_read(sessions, previous)
        async with sessions() as session:
            before = await snapshot(session)
        async with sessions.begin() as session:
            stats = await normalize_window(session, DAY, DAY)
            assert stats.inserted == stats.updated == 0
            assert await snapshot(session) == before
    async with sessions() as session:
        saved = await snapshot(session)
        assert len(saved[GarminRawRecord]) == 2
        assert len(saved[GarminRecordObservation]) == 4
        assert len(saved[GarminDailyProjection]) == 6  # Steps and distance outcomes.


@pytest.mark.parametrize("version", ["v1", "v2", "mixed"])
@pytest.mark.parametrize("observed", [False, True])
@pytest.mark.parametrize("negative", [-1, -0.0001, -1e-300, "-1e-10000", -(10**400)])
async def test_legacy_source_negatives_hide_stale_positive_normalized_rows(
    sessions, version, observed, negative
):
    body = {"calendarDate": str(DAY), "totalSteps": 7000}
    rows, _ = build_rows({"steps": {str(DAY): body}}, DAY, DAY, "Synthetic")
    async with sessions.begin() as session:
        for selected in ("v1", "v2") if version == "mixed" else (version,):
            values = [{**rows[Steps][0]}]
            if selected == "v1":
                values[0]["google_data_point_id"] = values[0]["google_data_point_id"].removesuffix(
                    ":v2"
                )
            await write_rows(session, Steps, values)
    await assert_read(sessions, 7000)  # No raw evidence: preserve legacy readability.
    for index, value in enumerate((7000, negative, 7000, negative)):
        if observed:
            await store_page(
                sessions, "steps", [{**body, "totalSteps": value}], NOW + timedelta(minutes=index)
            )
        elif index < 2:
            async with sessions.begin() as session:
                session.add(
                    GarminRawRecord(
                        kind="steps",
                        key=str(DAY),
                        body={**body, "totalSteps": value},
                        body_sha256=str(index) * 64,
                        first_seen_at=NOW + timedelta(minutes=index),
                    )
                )
        await assert_read(sessions, None if index % 2 or (not observed and index > 1) else 7000)
    async with sessions() as session:
        assert (await session.scalars(select(GarminDailyProjection))).all() == []
        assert set((await session.scalars(select(Steps.count))).all()) == {7000}


@pytest.mark.parametrize("garmin_first", [False, True])
async def test_negative_garmin_is_neither_selected_nor_an_alternative(sessions, garmin_first):
    await store(sessions, STEPS, steps(8000, NOW - timedelta(days=1), source=fitbit("Synthetic")))
    policy = Policy(health=("GARMIN", "FITBIT") if garmin_first else ("FITBIT", "GARMIN"))
    for index, value in enumerate((7000, -0.0001, 7000)):
        await store_page(
            sessions,
            "steps",
            [{"calendarDate": str(DAY), "totalSteps": value}],
            NOW + timedelta(minutes=index),
        )
        async with sessions.begin() as session:
            await normalize_window(session, DAY, DAY)
        async with sessions() as session:
            before = await snapshot(session)
            data = await dashboard_data(session, query=DashboardQuery(DAY, 1, "UTC"), policy=policy)
            day = data["days"][0]
            if index == 1:
                assert day["steps"] == 8000
                assert day["sources"]["steps"] == {
                    "source": "FITBIT",
                    "label": "Fitbit",
                    "alternatives": [],
                }
            else:
                assert day["steps"] == (7000 if garmin_first else 8000)
                assert len(day["sources"]["steps"]["alternatives"]) == 1
            assert await snapshot(session) == before


async def test_bounds_exclude_negative_edges_future_days_old_versions_and_quarantine(sessions):
    for offset, value in ((-5, -1), (-4, 0), (-3, 12), (-2, -0.0001), (-1, 90), (0, 100), (1, 80)):
        day = DAY + timedelta(days=offset)
        await store_page(
            sessions,
            "steps",
            [{"calendarDate": str(day), "totalSteps": value}],
            NOW,
        )
        async with sessions.begin() as session:
            await normalize_window(session, day, day)
    async with sessions.begin() as session:
        await session.execute(
            update(Steps)
            .where(Steps.source_record_id.in_((f"steps:{DAY - timedelta(days=1)}", f"steps:{DAY}")))
            .values(is_quarantined=True, quarantine_reason="Synthetic exclusion")
        )
        # A positive historical v1 row cannot hide its current negative v2 counterpart.
        row = (
            await session.scalars(
                select(Steps).where(Steps.source_record_id == f"steps:{DAY - timedelta(days=5)}")
            )
        ).one()
        session.add(
            Steps(
                **{
                    column.name: getattr(row, column.name)
                    for column in Steps.__table__.columns
                    if column.name != "id"
                }
                | {
                    "google_data_point_id": row.google_data_point_id.removesuffix(":v2"),
                    "count": 50,
                }
            )
        )
    async with sessions() as session:
        before = await snapshot(session)
        bounds = await archive_bounds(session, "steps", DAY, ZONE)
        assert bounds.earliest == DAY - timedelta(days=4)
        assert bounds.latest == DAY - timedelta(days=3)
        assert await daily_values(session, DAY, DAY, ZONE, metrics=("steps",)) == {}
        assert await snapshot(session) == before
