"""The Garmin pull: chunking, archive writes, versions, checkpoints, status and daily."""

from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from garmin_records import (
    TOKEN,
    FakeGarmin,
    activity,
    daily_steps,
    daily_stress,
    daily_summary,
    heart_rate,
    hrv_summary,
    sleep,
)
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from support.garmin import unlocked

from app.core.config import Settings
from app.garmin.client import REDACTED_BODY, GarminClient
from app.models.garmin import (
    GarminApiResponse,
    GarminHeartSet,
    GarminPullState,
    GarminRawRecord,
    GarminRecordObservation,
)
from app.models.health import ExerciseSession, Steps
from app.services.providers.garmin_pull import (
    BY_NAME,
    KIND_NAMES,
    cron_fields,
    plan,
    windows,
)

TODAY = date(2026, 11, 7)
STEPS_4_DAYS = (
    f"/usersummary-service/stats/steps/daily/{(TODAY - timedelta(days=3)).isoformat()}"
    f"/{TODAY.isoformat()}"
)
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
KEY = "synthetic-key"


def test_windows_chunk_inclusive_ranges_by_endpoint_limit():
    assert list(windows(date(2026, 1, 1), date(2026, 1, 1), 28)) == [(date(2026, 1, 1),) * 2]
    assert list(windows(date(2026, 1, 1), date(2026, 2, 28), 28)) == [
        (date(2026, 1, 1), date(2026, 1, 28)),
        (date(2026, 1, 29), date(2026, 2, 25)),
        (date(2026, 2, 26), date(2026, 2, 28)),
    ]
    assert list(windows(date(2026, 1, 2), date(2026, 1, 1), 28)) == []
    # Day kinds: one request per day; range kinds: their verified limits.
    year = (date(2025, 1, 1), date(2025, 12, 31))
    assert len(plan(BY_NAME["heart-rate"], *year)) == 365
    assert len(plan(BY_NAME["steps"], *year)) == 14  # 28-day chunks (29 answers 400)
    assert len(plan(BY_NAME["sleep-summaries"], *year)) == 12  # 31-day chunks
    assert len(plan(BY_NAME["daily-summaries"], *year)) == 5  # 90-day chunks
    assert len(plan(BY_NAME["activities"], *year)) == 5


def test_requests_follow_the_official_client_paths():
    day = date(2026, 11, 6)
    assert BY_NAME["steps"].request(day, day) == (
        "/usersummary-service/stats/steps/daily/2026-11-06/2026-11-06",
        {},
    )
    assert BY_NAME["daily-summaries"].request(day, day) == (
        "/usersummary-service/v2/dailysummaries",
        {"startDate": "2026-11-06", "endDate": "2026-11-06"},
    )
    assert BY_NAME["sleep"].request(day, day) == (
        "/sleep-service/sleep/dailySleep",
        {"date": "2026-11-06"},
    )
    assert BY_NAME["hrv"].request(day, day) == ("/hrv-service/hrv/2026-11-06", {})
    assert BY_NAME["spo2"].request(day, day) == (
        "/wellness-service/wellness/daily/spo2acclimation/2026-11-06",
        {},
    )
    assert BY_NAME["activities"].request(day, day)[0] == (
        "/activitylist-service/activities/search/activities"
    )
    assert len(KIND_NAMES) == 11


def test_cron_fields_skip_the_nightly_window():
    two_hours = cron_fields(120)
    assert two_hours == {"hour": "0,4,6,8,10,12,14,16,18,20,22", "minute": "10"}
    assert "2" not in cron_fields(60)["hour"].split(",")
    assert "3" not in cron_fields(60)["hour"].split(",")
    half_hour = cron_fields(30)
    assert half_hour["minute"] == "*/30"
    assert "2" not in half_hour["hour"].split(",") and "3" not in half_hour["hour"].split(",")
    assert cron_fields(1440) == {"hour": "0", "minute": "10"}


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def key_file(tmp_path):
    path = tmp_path / "key"
    path.write_text(KEY)
    return path


@pytest.fixture
def fake():
    fake = FakeGarmin()
    days = [TODAY - timedelta(days=offset) for offset in range(3, -1, -1)]
    fake.bodies["/usersummary-service/v2/dailysummaries"] = {
        "data": [daily_summary(day) for day in days]
    }
    fake.bodies[
        f"/usersummary-service/stats/steps/daily/{days[0].isoformat()}/{days[-1].isoformat()}"
    ] = [daily_steps(day) for day in days]
    fake.bodies[
        f"/usersummary-service/stats/stress/daily/{days[0].isoformat()}/{days[-1].isoformat()}"
    ] = [daily_stress(day) for day in days]
    fake.bodies["/wellness-service/wellness/dailyHeartRate"] = lambda request: heart_rate(
        date.fromisoformat(request.url.params["date"])
    )
    fake.bodies["/sleep-service/sleep/dailySleep"] = lambda request: sleep(
        date.fromisoformat(request.url.params["date"])
    )
    fake.bodies[f"/hrv-service/hrv/daily/{days[0].isoformat()}/{days[-1].isoformat()}"] = {
        "hrvSummaries": [hrv_summary(day) for day in days],
        "userProfilePk": 1,
    }
    fake.bodies["/activitylist-service/activities/search/activities"] = [
        activity(1001, days[1]),
        activity(1002, days[2]),
    ]
    return fake


def service(sessions, fake: FakeGarmin, key_file, clock: Clock, **settings):
    configured = Settings(
        _env_file=None, garmin_enabled=True, garmin_api_key_file=key_file, **settings
    )
    client = GarminClient(
        key_file,
        http=httpx.AsyncClient(transport=fake.transport()),
        sleep=clock.sleep,
        clock=clock,
    )
    return unlocked(configured, sessions, client=client, clock=clock)


async def count(sessions, model) -> int:
    async with sessions() as session:
        return await session.scalar(select(func.count()).select_from(model)) or 0


async def test_incremental_pull_archives_every_exchange_and_record(sessions, fake, key_file):
    clock = Clock()
    pull = service(sessions, fake, key_file, clock)
    summary = await pull.run()

    assert not summary.failed, summary.as_dict()
    # 7 range kinds take one request each over 4 days; 4 day kinds take 4 each.
    assert summary.as_dict()["requests"] == 7 + 4 * 4
    assert summary.kinds["daily-summaries"].new_records == 4
    assert summary.kinds["steps"].new_records == 4
    assert summary.kinds["heart-rate"].new_records == 4
    assert summary.kinds["sleep"].new_records == 4
    assert summary.kinds["activities"].new_records == 2
    assert summary.kinds["spo2"].new_records == 0  # the fake has no SpO2 body: empty list
    # Every HTTP exchange (token included) is kept; health bodies byte for byte, the token
    # exchange with a placeholder body (tests/test_garmin_token_redaction.py).
    assert await count(sessions, GarminApiResponse) == 1 + summary.as_dict()["requests"]
    async with sessions() as session:
        token_row = (
            await session.scalars(
                select(GarminApiResponse).where(GarminApiResponse.kind == "token")
            )
        ).one()
        assert token_row.method == "POST" and KEY not in token_row.body.decode()
        assert token_row.body == REDACTED_BODY and TOKEN not in token_row.body.decode()
        assert token_row.body_size == len(TOKEN) and token_row.status_code == 200
        rows = (await session.scalars(select(GarminRawRecord).order_by(GarminRawRecord.id))).all()
        by_kind = {}
        for row in rows:
            by_kind.setdefault(row.kind, []).append(row)
        assert [row.key for row in by_kind["steps"]] == [
            (TODAY - timedelta(days=offset)).isoformat() for offset in range(3, -1, -1)
        ]
        assert {row.key for row in by_kind["activities"]} == {"1001", "1002"}
        assert all(row.response_id is not None for row in rows)
        heart = by_kind["heart-rate"][0]
        assert heart.key == heart.body["calendarDate"]  # day kinds: the requested day
        states = (await session.scalars(select(GarminPullState))).all()
        assert {state.kind for state in states} == set(KIND_NAMES)
        assert all(
            state.last_success_at is not None and state.last_error is None for state in states
        )
    await pull.aclose()


async def test_refetching_keeps_versions_and_never_rewrites(sessions, fake, key_file):
    clock = Clock()
    pull = service(sessions, fake, key_file, clock)
    first = await pull.run(["steps"])
    assert first.kinds["steps"].new_records == 4
    before = await count(sessions, GarminRawRecord)

    clock.now += timedelta(hours=2)
    again = await pull.run(["steps"])
    assert again.kinds["steps"].records == 4 and again.kinds["steps"].new_records == 0
    assert await count(sessions, GarminRawRecord) == before  # same content: nothing added
    assert await count(sessions, GarminApiResponse) == 3  # token + two fetches, all kept

    path = STEPS_4_DAYS
    changed = [daily_steps(TODAY - timedelta(days=3 - i), steps=100 * (i + 1)) for i in range(4)]
    changed[-1]["totalSteps"] = 9999  # today's count grew during the day
    fake.bodies[path] = changed
    clock.now += timedelta(hours=2)
    third = await pull.run(["steps"])
    assert third.kinds["steps"].new_records == 4
    async with sessions() as session:
        today_rows = (
            await session.scalars(
                select(GarminRawRecord)
                .where(GarminRawRecord.kind == "steps", GarminRawRecord.key == TODAY.isoformat())
                .order_by(GarminRawRecord.id)
            )
        ).all()
        expected_steps = [daily_steps(TODAY)["totalSteps"], 9999]
        assert [row.body["totalSteps"] for row in today_rows] == expected_steps
        assert today_rows[0].first_seen_at < today_rows[1].first_seen_at
        with pytest.raises(DBAPIError, match="refused"):
            await session.execute(text("DELETE FROM garmin_raw_records"))
    async with sessions() as session:
        with pytest.raises(DBAPIError, match="refused"):
            await session.execute(text("UPDATE garmin_raw_records SET key = 'x'"))
    async with sessions() as session:
        with pytest.raises(DBAPIError, match="refused"):
            await session.execute(text("DELETE FROM garmin_api_responses"))
    await pull.aclose()


async def test_a_failing_kind_is_recorded_and_the_others_go_on(sessions, fake, key_file):
    clock = Clock()
    pull = service(sessions, fake, key_file, clock)
    fake.statuses["/sleep-service/sleep/dailySleep"] = [400]
    summary = await pull.run(["sleep", "steps"])
    assert summary.failed
    assert summary.kinds["sleep"].error.startswith("Garmin answered HTTP 400")
    assert summary.kinds["steps"].new_records == 4
    async with sessions() as session:
        state = await session.get(GarminPullState, "sleep")
        assert state.last_error.startswith("Garmin answered HTTP 400")
        assert state.last_error_at == state.last_attempt_at and state.last_success_at is None
    status = await pull.status()
    assert status["last_error"].startswith("Garmin answered HTTP 400")
    assert status["failing_kinds"] == ["sleep"]
    assert status["kinds"]["sleep"]["last_error_at"] is not None
    assert status["kinds"]["sleep"]["failing"] and not status["kinds"]["steps"]["failing"]
    assert status["kinds"]["steps"]["records"] == 4
    # The 400 exchange is archived like every other response.
    async with sessions() as session:
        failed = (
            await session.scalars(
                select(GarminApiResponse).where(GarminApiResponse.status_code == 400)
            )
        ).all()
        assert len(failed) == 1 and failed[0].kind == "sleep" and failed[0].body == b"{}"

    # Once the kind succeeds again its old error is kept but no longer counts as failing.
    fake.statuses["/sleep-service/sleep/dailySleep"] = []
    clock.now += timedelta(hours=1)
    recovered = await pull.run(["sleep"])
    assert not recovered.failed
    status = await pull.status()
    assert status["last_error"] is None and status["failing_kinds"] == []
    assert status["kinds"]["sleep"]["last_error"].startswith("Garmin answered HTTP 400")
    assert not status["kinds"]["sleep"]["failing"]
    await pull.aclose()


async def test_unstorable_record_is_counted_and_left_out(sessions, fake, key_file):
    path = STEPS_4_DAYS
    fake.bodies[path] = [daily_steps(TODAY), {"calendarDate": "2026-11-06", "note": "\u0000"}]
    pull = service(sessions, fake, key_file, Clock())
    summary = await pull.run(["steps"])
    assert summary.kinds["steps"].new_records == 1 and summary.kinds["steps"].unstorable == 1
    assert await count(sessions, GarminRawRecord) == 1
    await pull.aclose()


@pytest.mark.parametrize("malformed", [False, True])
async def test_backfill_chunks_checkpoints_and_resumes(sessions, fake, key_file, malformed):
    clock = Clock()
    pull = service(sessions, fake, key_file, clock)
    since, until = date(2026, 9, 1), date(2026, 10, 31)  # 61 days

    dry = await pull.backfill(since, until, kinds=["steps", "heart-rate"], dry_run=True)
    assert dry["dry_run"] and dry["kinds"]["steps"]["requests_planned"] == 3
    assert dry["kinds"]["heart-rate"]["requests_planned"] == 61
    assert fake.requests == [] and await count(sessions, GarminPullState) == 0

    def steps_body(request):
        start = date.fromisoformat(request.url.path.rsplit("/", 2)[1])
        return [daily_steps(start)]

    for chunk_start, _ in windows(since, until, 28):
        for chunk_end in (chunk_start + timedelta(days=27), until):
            fake.bodies[
                f"/usersummary-service/stats/steps/daily/{chunk_start}/{min(chunk_end, until)}"
            ] = steps_body
    # The second chunk fails on the first run.
    second = "/usersummary-service/stats/steps/daily/2026-09-29/2026-10-26"
    fake.statuses[second] = [] if malformed else [400]
    fake.bodies[second] = {"error": "synthetic"} if malformed else steps_body
    report = await pull.backfill(since, until, kinds=["steps"])
    assert report["failed"] and report["kinds"]["steps"]["requests"] == 2
    assert report["kinds"]["steps"]["new_records"] == 1
    async with sessions() as session:
        state = await session.get(GarminPullState, "steps")
        assert (state.backfill_since, state.backfill_until) == (since, until)
        assert state.backfill_next == date(2026, 9, 29)  # the chunk that failed

    fake.bodies[second] = steps_body
    resumed = await pull.backfill(since, until, kinds=["steps"])
    assert not resumed["failed"]
    assert resumed["kinds"]["steps"]["resumed_from"] == "2026-09-29"
    assert resumed["kinds"]["steps"]["requests"] == 2 and resumed["kinds"]["steps"]["complete"]
    async with sessions() as session:
        state = await session.get(GarminPullState, "steps")
        assert state.backfill_next is None and state.last_success_at is not None

    finished = await pull.backfill(since, until, kinds=["steps"])
    assert finished["kinds"]["steps"]["complete"] and finished["kinds"]["steps"]["requests"] == 0
    status = await pull.status()
    assert status["kinds"]["steps"]["backfill"] == {
        "since": "2026-09-01",
        "until": "2026-10-31",
        "next": None,
        "complete": True,
    }
    # Other bounds start over from their own first day.
    fresh = await pull.backfill(date(2026, 10, 1), until, kinds=["steps"])
    assert fresh["kinds"]["steps"]["resumed_from"] is None
    with pytest.raises(ValueError, match="unknown Garmin kind"):
        await pull.backfill(since, until, kinds=["nope"])
    with pytest.raises(ValueError, match="--until"):
        await pull.backfill(until, since)
    await pull.aclose()


async def test_status_and_daily_read_the_newest_record_per_day(sessions, fake, key_file):
    clock = Clock()
    pull = service(sessions, fake, key_file, clock)
    empty = await pull.status()
    assert empty["enabled"] and empty["key_file_present"] and empty["raw_records"] == 0
    assert empty["last_success_at"] is None and set(empty["kinds"]) == set(KIND_NAMES)

    await pull.run()
    status = await pull.status()
    assert (
        status["raw_records"] > 0
        and status["responses"]
        == status["raw_records"] - status["raw_records"] + status["responses"]
    )
    assert status["kinds"]["steps"]["newest_key"] == TODAY.isoformat()
    assert status["last_success_at"] == clock.now.isoformat()
    assert status["last_error"] is None

    daily = await pull.daily(TODAY - timedelta(days=1), TODAY)
    assert daily["source"] == "garmin" and len(daily["days"]) == 2
    today = daily["days"][-1]
    assert today["date"] == TODAY.isoformat()
    assert today["steps"] == daily_steps(TODAY)["totalSteps"]
    assert today["resting_heart_rate"] == heart_rate(TODAY)["restingHeartRate"]
    assert today["sleep_seconds"] == sleep(TODAY)["sleepTimeSeconds"]
    assert today["sleep_score"] == sleep(TODAY)["sleepScores"]["overall"]["value"]
    assert today["sleep_stages_seconds"]["deep"] == sleep(TODAY)["deepSleepSeconds"]
    assert today["body_battery"] == {
        name: daily_summary(TODAY)["bodyBattery"][f"{name}Value"]
        for name in ("max", "min", "latest", "charged", "drained")
    }
    assert today["stress_average"] == daily_stress(TODAY)["values"]["overallStressLevel"]
    assert today["hrv_last_night_average"] == hrv_summary(TODAY)["lastNightAvg"]
    assert today["spo2_average"] is None  # no SpO2 record

    # A later version of today's steps wins; the earlier row stays.
    path = STEPS_4_DAYS
    fake.bodies[path] = [daily_steps(TODAY, steps=12_000)]
    clock.now += timedelta(hours=1)
    await pull.run(["steps"])
    daily = await pull.daily(TODAY, TODAY)
    assert daily["days"][0]["steps"] == 12_000
    assert await count(sessions, GarminRawRecord) > 0
    await pull.aclose()


async def test_disabled_runtime_has_no_pull_and_enabled_runtime_has_one(sessions, key_file):
    from app.runtime import build_runtime

    off = build_runtime(Settings(_env_file=None), sessions)
    try:
        assert off.garmin is None
    finally:
        await off.shutdown()
    on = build_runtime(
        Settings(_env_file=None, garmin_enabled=True, garmin_api_key_file=key_file), sessions
    )
    try:
        assert on.garmin is not None and on.garmin.enabled
        assert (await on.garmin.status())["key_file_present"]
    finally:
        await on.shutdown()


@pytest.mark.parametrize("kind", ["steps", "activities"])
async def test_scheduled_pull_reobserves_old_content_without_rewriting_evidence(
    sessions, fake, key_file, kind
):
    clock = Clock()
    pull = service(sessions, fake, key_file, clock)
    model = Steps if kind == "steps" else ExerciseSession
    path = STEPS_4_DAYS if kind == "steps" else "/activitylist-service/activities/search/activities"
    evidence = (GarminApiResponse, GarminRawRecord, GarminRecordObservation)
    before = {table: [] for table in evidence}
    normalized_before = None
    for index, value in enumerate((100, 200, 100, 100)):
        # The repeated A has the same timestamp as the preceding B: observation id breaks ties.
        if index in (1, 3):
            clock.now += timedelta(minutes=1)
        body = (
            daily_steps(TODAY, steps=value)
            if kind == "steps"
            else {**activity(1, TODAY), "calories": value}
        )
        fake.bodies[path] = [body]
        summary = await pull.run([kind])
        assert not summary.failed
        assert summary.kinds[kind].new_records == (1 if index < 2 else 0)
        table_stats = summary.normalized.tables[model.__tablename__]
        assert table_stats["updated"] == (1 if index in (1, 2) else 0)
        assert table_stats["unchanged"] == (1 if index == 3 else 0)
        async with sessions() as session:
            normalized = (await session.execute(select(model.__table__))).one()
            if index == 3:
                assert normalized == normalized_before, "an unchanged A must not touch updated_at"
            normalized_before = normalized
            row = (await session.scalars(select(model))).one()
            assert float(row.count if kind == "steps" else row.calories_kcal) == value
            for table in evidence:
                rows = (await session.execute(select(table.__table__).order_by(table.id))).all()
                assert rows[: len(before[table])] == before[table]
                before[table] = rows
        if kind == "steps":
            assert (await pull.daily(TODAY, TODAY))["days"][0]["steps"] == value
        if index == 1:
            # Restart the service; the choice must come from committed evidence, not memory.
            await pull.aclose()
            pull = service(sessions, fake, key_file, clock)
    assert len(before[GarminRawRecord]) == 2
    observations = before[GarminRecordObservation]
    assert len(observations) == 4
    assert [row.raw_record_id for row in observations] == [
        before[GarminRawRecord][i].id for i in (0, 1, 0, 0)
    ]
    responses = {row.id: row for row in before[GarminApiResponse]}
    assert len({row.response_id for row in observations}) == 4
    assert all(responses[row.response_id].kind == kind for row in observations)
    await pull.aclose()
    # Backfill after another restart returns the same current values and writes nothing.
    pull = service(sessions, fake, key_file, clock)
    report = await pull.backfill(TODAY, TODAY, kinds=[kind])
    assert not report["failed"] and report["normalized"]["updated"] == 0
    assert await count(sessions, GarminRawRecord) == 2
    await pull.aclose()


@pytest.mark.parametrize("failed_value", [100, 300])
async def test_failed_page_keeps_response_but_rolls_back_raw_versions_and_observations(
    sessions, fake, key_file, monkeypatch, failed_value
):
    clock = Clock()
    pull = service(sessions, fake, key_file, clock)
    for value in (100, 200):
        fake.bodies[STEPS_4_DAYS] = [daily_steps(TODAY, steps=value)]
        await pull.run(["steps"])
        clock.now += timedelta(minutes=1)
    before = await count(sessions, GarminApiResponse)
    real = pull.backend.store_raw

    async def interrupted(*args, **kwargs):
        await real(*args, **kwargs)
        raise RuntimeError("synthetic page failure after observations")

    monkeypatch.setattr(pull.backend, "store_raw", interrupted)
    fake.bodies[STEPS_4_DAYS] = [daily_steps(TODAY, steps=failed_value)]
    failed = await pull.run(["steps"])
    assert failed.failed and failed.normalized.updated == 0
    assert await count(sessions, GarminRawRecord) == 2
    assert await count(sessions, GarminRecordObservation) == 2
    assert await count(sessions, GarminApiResponse) == before + 1
    assert (await pull.daily(TODAY, TODAY))["days"][0]["steps"] == 200
    async with sessions() as session:
        assert (await session.scalars(select(Steps))).one().count == 200
        kept = await session.scalar(select(GarminApiResponse).order_by(GarminApiResponse.id.desc()))
        assert str(failed_value).encode() in kept.body
        assert not await session.scalar(
            select(GarminRecordObservation.id).where(GarminRecordObservation.response_id == kept.id)
        )
    monkeypatch.setattr(pull.backend, "store_raw", real)
    recovered = await pull.run(["steps"])
    assert not recovered.failed and recovered.normalized.tables["steps"]["updated"] == 1
    assert (await pull.daily(TODAY, TODAY))["days"][0]["steps"] == failed_value
    await pull.aclose()


async def test_record_page_without_response_cannot_publish_an_observation(sessions):
    from app.services.providers.garmin_ingestion import GarminPage, GarminSource

    source = GarminSource(Settings(_env_file=None), sessions, clock=Clock())
    with pytest.raises(ValueError, match="observations require a response"):
        async with sessions.begin() as session:
            await source.store_raw(
                session, BY_NAME["steps"], GarminPage([daily_steps(TODAY)], None, [])
            )
    for model in (GarminRawRecord, GarminRecordObservation, GarminApiResponse):
        assert await count(sessions, model) == 0


@pytest.mark.parametrize("backfill", [False, True])
@pytest.mark.parametrize("collection", ["valid", 123, True, False, "text", {}, None, []])
async def test_pull_and_backfill_publish_heart_membership_after_restart(
    sessions, fake, key_file, backfill, collection
):
    from sqlalchemy import true

    from app.services.providers.ingestion_readers import GARMIN_ARCHIVE

    garmin_samples = GARMIN_ARCHIVE.heart_rate
    assert garmin_samples is not None

    clock = Clock()
    path = "/wellness-service/wellness/dailyHeartRate"
    a = heart_rate(TODAY)
    ms = int(datetime.combine(TODAY, datetime.min.time(), UTC).timestamp() * 1000)
    a["heartRateValues"] = [[ms, 60], [ms + 60_000, 70]]
    b = {**a, "heartRateValues": a["heartRateValues"][:1]}
    if collection != "valid":
        b["heartRateValues"] = collection
    start = datetime.combine(TODAY, datetime.min.time(), UTC) - timedelta(days=1)
    stop = start + timedelta(days=2)
    for index, body in enumerate((a, b, b, a)):
        # Re-create the service on each run: membership must survive process restarts.
        pull = service(sessions, fake, key_file, clock, garmin_lookback_days=1)
        fake.bodies[path] = lambda request, body=body: (
            body if request.url.params["date"] == str(TODAY) else {}
        )
        if backfill:
            report = await pull.backfill(
                TODAY - timedelta(days=index % 2), TODAY, kinds=["heart-rate"]
            )
            assert not report["failed"]
        else:
            assert not (await pull.run(["heart-rate"])).failed
        async with sessions() as session:
            read = await garmin_samples(session, start, stop, true())
            expected = [60] if collection == "valid" else []
            expected = [60, 70] if index in (0, 3) else expected
            assert [bpm for _, bpm, _ in read.samples] == expected
            assert (
                await session.scalar(select(func.count()).select_from(GarminHeartSet))
                == (1, 2, 2, 3)[index]
            )
        clock.now += timedelta(minutes=1)
        await pull.aclose()
    assert await count(sessions, GarminRawRecord) == 2
    assert await count(sessions, GarminRecordObservation) == 4
    # A successful response with no day record is not a source deletion.
    fake.bodies[path] = {}
    pull = service(sessions, fake, key_file, clock, garmin_lookback_days=1)
    assert not (await pull.run(["heart-rate"])).failed
    fake.statuses[path] = [503] * 10
    assert (await pull.run(["heart-rate"])).failed
    async with sessions() as session:
        read = await garmin_samples(session, start, stop, true())
        assert [bpm for _, bpm, _ in read.samples] == [60, 70]
        assert await session.scalar(select(func.count()).select_from(GarminHeartSet)) == 3
    await pull.aclose()
