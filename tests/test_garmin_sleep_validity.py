"""Published Garmin sleep signs cannot be hidden by SQL rounding or fallbacks."""

from datetime import timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from derived_records import sleep, store
from garmin_records import store_page
from sqlalchemy import event, select, update
from test_garmin_normalize import DAY, NOW, garmin_sleep
from test_garmin_step_validity import only_select

from app.models.garmin import (
    GarminApiResponse,
    GarminDailyProjection,
    GarminRawRecord,
    GarminRecordObservation,
)
from app.models.health import SleepSession
from app.services.dashboard_views.dashboard import dashboard_data
from app.services.identities.source_precedence import Policy
from app.services.providers.garmin_normalize import build_rows, normalize_window, write_rows
from app.services.providers.garmin_readers import archive_bounds, daily_values
from app.services.providers.garmin_sleep_values import _negative
from app.services.readers.dashboard_queries import DashboardQuery

ZONE = ZoneInfo("UTC")
TABLES = (
    GarminApiResponse,
    GarminRawRecord,
    GarminRecordObservation,
    GarminDailyProjection,
    SleepSession,
)
FIELDS = ("sleepTimeSeconds", "awakeSleepSeconds")


async def snapshot(session):
    return {
        model: (await session.execute(select(model.__table__).order_by(model.id))).all()
        for model in TABLES
    }


async def assert_read(sessions, expected):
    engine = sessions.kw["bind"].sync_engine
    async with sessions() as session:
        before = await snapshot(session)
        event.listen(engine, "before_cursor_execute", only_select)
        try:
            days = await daily_values(session, DAY, DAY, ZONE, metrics=("sleep_seconds",))
            assert days == ({} if expected is None else {DAY: {"sleep_seconds": expected}})
            bounds = await archive_bounds(session, "sleep", DAY, ZONE)
            assert bounds.earliest == bounds.latest == (None if expected is None else DAY)
        finally:
            event.remove(engine, "before_cursor_execute", only_select)
        assert await snapshot(session) == before


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("kind", ["sleep", "sleep-summaries"])
@pytest.mark.parametrize(
    "value",
    [-1, -0.0001, -1e-300, "-0.0001", "-1e-10000", -(10**400), 0, -0.0, "-0e-10000", "1200"],
)
async def test_published_sign_survives_pending_observations_and_reversion(
    sessions, field, kind, value
):
    valid = garmin_sleep(DAY)
    invalid = Decimal(str(value)) < 0
    changed = (
        None
        if invalid
        else (max(0, float(value) - 1200) if field == FIELDS[0] else 28800 - float(value))
    )
    previous = None
    archived = {model: [] for model in TABLES}
    for index, payload in enumerate((valid, {**valid, field: value}, valid)):
        await store_page(sessions, kind, [payload], NOW + timedelta(minutes=index))
        await assert_read(sessions, previous)  # Pending inputs cannot change publication.
        async with sessions.begin() as session:
            await normalize_window(session, DAY, DAY)
        previous = changed if index == 1 else 27600
        await assert_read(sessions, previous)
        async with sessions() as session:
            before = await snapshot(session)
            # Normalization retains its existing in-place measurement updates;
            # raw evidence and completed projection history are append-only.
            for model in TABLES[:-1]:
                assert before[model][: len(archived[model])] == archived[model]
            archived = before
        async with sessions.begin() as session:
            stats = await normalize_window(session, DAY, DAY)
            assert stats.inserted == stats.updated == 0
            assert await snapshot(session) == before
    async with sessions() as session:
        saved = await snapshot(session)
        assert len(saved[GarminRawRecord]) == 2
        assert len(saved[GarminRecordObservation]) == len(saved[GarminDailyProjection]) == 3
        assert len(saved[SleepSession]) <= 2


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("version", ["v1", "v2", "mixed"])
@pytest.mark.parametrize("observed", [False, True])
@pytest.mark.parametrize("negative", [-1, -0.0001, "-1e-10000"])
async def test_legacy_version_choice_and_normalized_only_rows(
    sessions, field, version, observed, negative
):
    body = garmin_sleep(DAY)
    rows, _ = build_rows({"sleep": {str(DAY): body}}, DAY, DAY, "Synthetic")
    async with sessions.begin() as session:
        for selected in ("v1", "v2") if version == "mixed" else (version,):
            values = {**rows[SleepSession][0]}
            if selected == "v1":
                values["google_data_point_id"] = values["google_data_point_id"].removesuffix(":v2")
                values["duration_seconds"] = 20000
            await write_rows(session, SleepSession, [values])
    expected = 18800 if version == "v1" else 27600
    await assert_read(sessions, expected)
    for index, value in enumerate((28800, negative, 28800)):
        if observed:
            await store_page(
                sessions, "sleep", [{**body, field: value}], NOW + timedelta(minutes=index)
            )
        elif index < 2:
            async with sessions.begin() as session:
                session.add(
                    GarminRawRecord(
                        kind="sleep",
                        key=str(DAY),
                        body={**body, field: value},
                        body_sha256=str(index) * 64,
                        first_seen_at=NOW,
                    )
                )
        await assert_read(sessions, None if index == 1 or not observed and index == 2 else expected)
    async with sessions() as session:
        assert not (await session.scalars(select(GarminDailyProjection))).all()


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("missing", [True, False])
async def test_missing_selected_members_keep_interval_and_awake_fallback(sessions, field, missing):
    body = garmin_sleep(DAY)
    if missing:
        body.pop(field)
    else:
        body[field] = None
    # A present primary must never borrow the alternate's negative member or
    # interpret Google-shaped fields in the Garmin body as sleep evidence.
    body["summary"] = {"sleepTime": "-1s"}
    body["sleepStages"] = [{"stage": "AWAKE", "duration": "-1s"}]
    await store_page(sessions, "sleep", [body], NOW)
    await store_page(sessions, "sleep-summaries", [{**garmin_sleep(DAY), field: -1}], NOW)
    async with sessions.begin() as session:
        await normalize_window(session, DAY, DAY)
    await assert_read(sessions, 27600 if field == FIELDS[0] else 28800)


@pytest.mark.parametrize("field", FIELDS)
async def test_negative_primary_cannot_use_alternate_or_other_session_on_wake_day(sessions, field):
    await store_page(sessions, "sleep", [{**garmin_sleep(DAY), field: -0.0001}], NOW)
    # The alternate is valid and ends on the same wake day, under another day key.
    other = {**garmin_sleep(DAY), "calendarDate": str(DAY - timedelta(days=1))}
    await store_page(sessions, "sleep-summaries", [garmin_sleep(DAY), other], NOW)
    async with sessions.begin() as session:
        await normalize_window(session, DAY - timedelta(days=1), DAY)
    await assert_read(sessions, None)


@pytest.mark.parametrize("field", ["deepSleepSeconds", "lightSleepSeconds", "remSleepSeconds"])
async def test_unused_negative_stage_does_not_change_daily_total(sessions, field):
    await store_page(sessions, "sleep", [{**garmin_sleep(DAY), field: -1}], NOW)
    async with sessions.begin() as session:
        await normalize_window(session, DAY, DAY)
    await assert_read(sessions, 27600)


async def test_demo_quarantine_and_normalized_negatives_keep_existing_rules(sessions):
    await store_page(sessions, "sleep", [{**garmin_sleep(DAY), FIELDS[0]: -0.0001}], NOW)
    async with sessions.begin() as session:
        await normalize_window(session, DAY, DAY)
        prepared, _ = build_rows({"sleep": {str(DAY): garmin_sleep(DAY)}}, DAY, DAY, "Synthetic")
        await write_rows(session, SleepSession, [{**prepared[SleepSession][0], "provider": "DEMO"}])
    async with sessions() as session:
        assert (await daily_values(session, DAY, DAY, ZONE, provider="DEMO"))[DAY][
            "sleep_seconds"
        ] == 27600
    async with sessions.begin() as session:
        await session.execute(
            update(SleepSession).where(SleepSession.provider == "DEMO").values(awake_seconds=-1)
        )
    async with sessions() as session:
        assert (
            await daily_values(session, DAY, DAY, ZONE, metrics=("sleep_seconds",), provider="DEMO")
            == {}
        )
    await store_page(sessions, "sleep", [garmin_sleep(DAY)], NOW + timedelta(minutes=1))
    async with sessions.begin() as session:
        await normalize_window(session, DAY, DAY)
        await session.execute(update(SleepSession).values(is_quarantined=True))
    await assert_read(sessions, None)


async def test_sleep_read_is_one_select_of_two_raw_members_for_many_days(sessions):
    for offset in range(32):
        day = DAY - timedelta(days=offset)
        body = {**garmin_sleep(day), "unread": "x" * 65536}
        body["sleepStartTimestampGMT"] -= offset * 86400000
        body["sleepEndTimestampGMT"] -= offset * 86400000
        await store_page(sessions, "sleep", [body], NOW)
    first = DAY - timedelta(days=31)
    async with sessions.begin() as session:
        await normalize_window(session, first, DAY)
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        only_select(conn, cursor, statement, parameters, context, executemany)
        statements.append(statement)

    engine = sessions.kw["bind"].sync_engine
    event.listen(engine, "before_cursor_execute", capture)
    try:
        async with sessions() as session:
            days = await daily_values(session, first, DAY, ZONE, metrics=("sleep_seconds",))
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert len(days) == 32 and all(d["sleep_seconds"] == 27600 for d in days.values())
    assert len(statements) == 1
    selected = statements[0].split("FROM", 1)[0]
    assert "sleep.body ->" in selected and "sleep_summaries.body ->" in selected
    assert "sleep.body," not in selected and "sleep_summaries.body," not in selected


@pytest.mark.parametrize(
    "token,negative",
    [
        (" -1e-10000 ", True),
        ('"-1e-10000"', True),
        ("-0e-10000", False),
        ('"-0e-10000"', False),
        ("1e-10000", False),
        ("null", False),
        ("true", False),
    ],
)
def test_json_numeric_tokens_and_text_keep_the_sign_before_float_conversion(token, negative):
    assert _negative(token) is negative


@pytest.mark.parametrize("primary", [None, {}])
async def test_empty_primary_keeps_whole_record_alternate_selection(sessions, primary):
    if primary is not None:
        async with sessions.begin() as session:
            session.add(
                GarminRawRecord(
                    kind="sleep",
                    key=str(DAY),
                    body=primary,
                    body_sha256="a" * 64,
                    first_seen_at=NOW,
                )
            )
    for index, value in enumerate((28800, -0.0001, 28800)):
        await store_page(
            sessions,
            "sleep-summaries",
            [{**garmin_sleep(DAY), "sleepTimeSeconds": value}],
            NOW + timedelta(minutes=index),
        )
        async with sessions.begin() as session:
            await normalize_window(session, DAY, DAY)
        await assert_read(sessions, None if index == 1 else 27600)


async def test_pending_primary_does_not_replace_published_alternate(sessions):
    await store_page(sessions, "sleep-summaries", [garmin_sleep(DAY)], NOW)
    async with sessions.begin() as session:
        await normalize_window(session, DAY, DAY)
    await store_page(
        sessions,
        "sleep",
        [{**garmin_sleep(DAY), "awakeSleepSeconds": -0.0001}],
        NOW + timedelta(minutes=1),
    )
    await assert_read(sessions, 27600)
    async with sessions.begin() as session:
        await normalize_window(session, DAY, DAY)
    await assert_read(sessions, None)


@pytest.mark.parametrize("garmin_first", [False, True])
async def test_invalid_sleep_leaves_google_eligible_without_garmin_alternative(
    sessions, garmin_first
):
    start = NOW.replace(day=6, hour=0)
    await store(
        sessions,
        "sleep",
        sleep(start=start, end=start + timedelta(hours=6), summary={"minutesAsleep": 300}),
    )
    policy = Policy(health=("GARMIN", "FITBIT") if garmin_first else ("FITBIT", "GARMIN"))
    for index, value in enumerate((28800, -0.0001, 28800)):
        await store_page(
            sessions,
            "sleep",
            [{**garmin_sleep(DAY), "sleepTimeSeconds": value}],
            NOW + timedelta(minutes=index),
        )
        async with sessions.begin() as session:
            await normalize_window(session, DAY, DAY)
        async with sessions() as session:
            before = await snapshot(session)
            data = await dashboard_data(session, query=DashboardQuery(DAY, 1, "UTC"), policy=policy)
            day = data["days"][0]
            if index == 1:
                assert day["sleep_seconds"] == 18000
                assert day["sources"]["sleep_seconds"] == {
                    "source": "FITBIT",
                    "label": "Fitbit",
                    "alternatives": [],
                }
            else:
                assert day["sleep_seconds"] == (27600 if garmin_first else 18000)
                assert len(day["sources"]["sleep_seconds"]["alternatives"]) == 1
            assert await snapshot(session) == before
