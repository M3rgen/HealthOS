"""Outer response contracts fail safely; empty bodies and record contents keep their rules."""

import hashlib
import json
from datetime import timedelta

import httpx
import pytest
from garmin_records import TOKEN, daily_steps, heart_rate, hrv_summary, sleep
from sqlalchemy import select
from test_garmin_pagination import evidence
from test_garmin_pagination import paginated as paginated
from test_garmin_pull import KEY, TODAY
from test_garmin_pull import key_file as key_file

from app.garmin.client import REDACTED_BODY
from app.models.garmin import GarminApiResponse, GarminRawRecord, GarminRecordObservation
from app.models.health import (
    HeartRateVariability,
    OxygenSaturation,
    RestingHeartRate,
    SleepSession,
    Steps,
)
from app.services.providers.garmin_kinds import (
    KINDS,
    GarminResponseShapeError,
    GarminSingleResponseShapeError,
)
from app.services.providers.garmin_pull import BY_NAME, day_window

LIST_KINDS = [kind.name for kind in KINDS if kind.mode != "day"]
DAY_KINDS = [kind.name for kind in KINDS if kind.mode == "day"]
FIELDS = {"daily-summaries": "data", "hrv-summaries": "hrvSummaries"}
INVALID_SINGLE = [0, 42, 1.5, False, True, "", f"synthetic {KEY} {TOKEN}", [{"error": TOKEN}]]
SINGLE_ERROR = "Garmin single-day response has an unexpected structure"
HEALTHY = {
    "heart-rate": (heart_rate(TODAY), RestingHeartRate),
    "spo2": ({"averageSpO2": 97}, OxygenSaturation),
    "sleep": (sleep(TODAY), SleepSession),
    "hrv": ({"hrvSummary": hrv_summary(TODAY)}, HeartRateVariability),
}


async def answer(pull, fake, status, body, kind=None):
    path, params = BY_NAME[kind].request(TODAY, TODAY) if kind else (None, {})

    def respond(request):
        response = fake(request)
        if request.method == "POST" or (
            kind and (request.url.path != path or dict(request.url.params) != params)
        ):
            return response
        return httpx.Response(status, content=body)

    await pull.client.http.aclose()
    pull.client.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))


@pytest.mark.parametrize("kind", LIST_KINDS)
@pytest.mark.parametrize("body", [{"error": "synthetic"}, {}, 42, 0, False, True, "", "synthetic"])
async def test_invalid_outer_shape_archived_before_safe_typed_failure(
    sessions, paginated, kind, body, caplog
):
    pull, fake, _ = paginated
    raw = json.dumps(body).encode()
    await answer(pull, fake, 200, raw)
    with pytest.raises(GarminResponseShapeError) as error:
        async for _ in pull.source.fetch(BY_NAME[kind], day_window(TODAY, TODAY)):
            pytest.fail("Invalid envelopes must never yield a record page")
    assert str(error.value) == "Garmin list response has an unexpected structure"
    assert error.value.__cause__ is None and error.value.__context__ is None
    assert "synthetic" not in caplog.text
    kept = await evidence(sessions)
    assert len(kept[GarminApiResponse]) == 2  # Token and the refused outer shape, once each.
    assert kept[GarminApiResponse][-1].body == raw
    assert not kept[GarminRawRecord] and not kept[GarminRecordObservation]


@pytest.mark.parametrize("kind", FIELDS)
async def test_populated_list_cannot_replace_a_named_envelope(paginated, kind):
    pull, fake, _ = paginated
    await answer(pull, fake, 200, b"[{}]")
    with pytest.raises(GarminResponseShapeError):
        async for _ in pull.source.fetch(BY_NAME[kind], day_window(TODAY, TODAY)):
            pytest.fail("A populated list is not the endpoint's envelope")


@pytest.mark.parametrize("kind,field", FIELDS.items())
@pytest.mark.parametrize("value", [None, {}, False, 0, "synthetic"])
@pytest.mark.parametrize("echo", [False, True])
async def test_envelope_field_must_be_a_list(sessions, paginated, kind, field, value, echo, caplog):
    pull, fake, _ = paginated
    raw = json.dumps({field: value, "error": TOKEN if echo else "synthetic"}).encode()
    await answer(pull, fake, 200, raw)
    failed = await pull.run([kind])
    assert failed.failed and failed.kinds[kind].records == 0
    status = await pull.status()
    assert status["failing_kinds"] == [kind] and status["last_success_at"] is None
    assert status["last_error"] == (
        "Garmin response contained a credential"
        if echo
        else "Garmin list response has an unexpected structure"
    )
    assert TOKEN not in caplog.text and "synthetic" not in caplog.text
    kept = await evidence(sessions)
    response = kept[GarminApiResponse][-1]
    assert response.body == (REDACTED_BODY if echo else raw) and response.body_size == len(raw)
    assert response.body_sha256 == hashlib.sha256(raw).hexdigest()
    assert not kept[GarminRawRecord] and not kept[GarminRecordObservation]


@pytest.mark.parametrize("kind", LIST_KINDS + DAY_KINDS)
@pytest.mark.parametrize(
    "status,raw", [(204, b""), (200, b""), (200, b" \n"), (200, b"null"), (200, b"[]"), (200, None)]
)
async def test_supported_empty_http_answers_complete(sessions, paginated, kind, status, raw):
    pull, fake, _ = paginated
    if raw is None:
        raw = json.dumps(
            {FIELDS[kind]: []} if kind in FIELDS else {} if kind in DAY_KINDS else []
        ).encode()
    await answer(pull, fake, status, raw)
    report = await pull.backfill(TODAY, TODAY, kinds=[kind])
    assert not report["failed"] and report["kinds"][kind]["complete"]
    assert report["kinds"][kind]["requests"] == 1 and report["kinds"][kind]["records"] == 0
    state = (await pull.status())["kinds"][kind]
    assert state["last_success_at"] is not None and state["backfill"]["complete"]
    kept = await evidence(sessions)
    assert len(kept[GarminApiResponse]) == 2 and kept[GarminApiResponse][-1].body == raw
    assert not kept[GarminRawRecord] and not kept[GarminRecordObservation]


async def test_list_members_still_archive_and_normalize_independently(sessions, paginated):
    pull, fake, _ = paginated
    records = [
        daily_steps(TODAY),
        {"calendarDate": str(TODAY - timedelta(days=1)), "totalSteps": {}},
        None,
        42,
    ]
    raw = json.dumps(records).encode()
    await answer(pull, fake, 200, raw)
    summary = await pull.run(["steps"])
    assert not summary.failed and summary.kinds["steps"].records == 4
    kept = await evidence(sessions)
    assert [row.body for row in kept[GarminRawRecord]] == records
    assert len(kept[GarminRecordObservation]) == 4
    assert kept[GarminApiResponse][-1].body == raw
    async with sessions() as session:
        assert (await session.scalars(select(Steps))).one().count == 7000


@pytest.mark.parametrize("kind", DAY_KINDS)
@pytest.mark.parametrize("body", INVALID_SINGLE)
def test_single_day_invalid_shape_has_safe_typed_failure(kind, body):
    with pytest.raises(GarminSingleResponseShapeError, match=SINGLE_ERROR) as error:
        BY_NAME[kind].records(body)
    assert error.value.__cause__ is None and error.value.__context__ is None


@pytest.mark.parametrize("kind", DAY_KINDS)
@pytest.mark.parametrize("body", [{"error": "synthetic"}, {"averageSpO2": {}}, {"unknown": 42}])
async def test_single_day_objects_archive_without_measurements(sessions, paginated, kind, body):
    pull, fake, _ = paginated
    await answer(pull, fake, 200, json.dumps(body).encode(), kind)
    report = await pull.backfill(TODAY, TODAY, kinds=[kind])
    assert not report["failed"] and report["kinds"][kind]["records"] == 1
    assert report["normalized"]["inserted"] == 0
    kept = await evidence(sessions)
    assert [row.body for row in kept[GarminRawRecord]] == [body]
    assert len(kept[GarminRecordObservation]) == 1


@pytest.mark.parametrize("kind", DAY_KINDS)
@pytest.mark.parametrize("body", INVALID_SINGLE)
@pytest.mark.parametrize("backfill", [False, True])
async def test_single_day_failure_preserves_day_and_retries(
    sessions, paginated, kind, body, backfill, caplog
):
    pull, fake, clock = paginated
    pull.settings.garmin_lookback_days = 1
    healthy, model = HEALTHY[kind]
    await answer(pull, fake, 200, json.dumps(healthy).encode(), kind)
    assert not (await pull.run([kind])).failed
    before, success = await evidence(sessions), clock.now.isoformat()
    clock.now += timedelta(minutes=1)
    raw = json.dumps(body).encode()
    await answer(pull, fake, 200, raw, kind)
    since = TODAY - timedelta(days=1)
    if backfill:
        result = await pull.backfill(since, TODAY, kinds=[kind, "steps"])
        entry = result["kinds"][kind]
        assert result["failed"] and not entry["complete"]
    else:
        result = await pull.run([kind, "steps"])
        entry = result.kinds[kind].as_dict()
        assert result.failed
    error = "Garmin response contained a credential" if TOKEN in str(body) else SINGLE_ERROR
    assert entry["error"] == error and entry["requests"] == 2
    assert entry["records"] == entry["new_records"] == 0
    status = await pull.status()
    state = status["kinds"][kind]
    assert status["failing_kinds"] == [kind] and status["last_error"] == error
    assert state["last_success_at"] == success
    assert success < state["last_error_at"] <= clock.now.isoformat()
    assert status["kinds"]["steps"]["last_success_at"] == clock.now.isoformat()
    if backfill:
        assert state["backfill"]["next"] == str(TODAY) and not state["backfill"]["complete"]
    after = await evidence(sessions)
    for model_type in (GarminRawRecord, GarminRecordObservation):
        assert after[model_type] == before[model_type]
    (response,) = [
        r for r in after[GarminApiResponse] if r.body_sha256 == hashlib.sha256(raw).hexdigest()
    ]
    assert response.kind == kind
    assert response.status_code == 200 and response.complete and response.body_size == len(raw)
    assert response.body == (REDACTED_BODY if TOKEN in str(body) else raw)
    logged = str([record.__dict__ for record in caplog.records])
    assert KEY not in logged and TOKEN not in logged and "synthetic" not in logged
    clock.now += timedelta(minutes=1)
    fake.requests.clear()
    await answer(pull, fake, 200, json.dumps(healthy).encode(), kind)
    if backfill:
        recovered = await pull.backfill(since, TODAY, kinds=[kind, "steps"])
        assert not recovered["failed"] and recovered["kinds"][kind]["complete"]
        assert recovered["kinds"][kind]["resumed_from"] == str(TODAY)
        path, params = BY_NAME[kind].request(TODAY, TODAY)
        assert [(r.url.path, dict(r.url.params)) for r in fake.requests] == [(path, params)]
    else:
        assert not (await pull.run([kind, "steps"])).failed
    status = await pull.status()
    assert status["failing_kinds"] == []
    assert status["kinds"][kind]["last_success_at"] > state["last_error_at"]
    kept = await evidence(sessions)
    assert kept[GarminRawRecord] == before[GarminRawRecord]
    assert len(kept[GarminRecordObservation]) == len(before[GarminRecordObservation]) + 1
    async with sessions() as session:
        assert len((await session.scalars(select(model))).all()) == 1
