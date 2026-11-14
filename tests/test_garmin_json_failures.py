"""Real JSON decoder failures retain HTTP evidence and resumable pull state."""

import asyncio
import hashlib
import json
import traceback
from datetime import timedelta

import httpx
import pytest
from garmin_records import TOKEN, hrv_summary
from garmin_token_retries import RetryingGarmin
from sqlalchemy import select
from test_garmin_pagination import PATH as ACTIVITY_PATH
from test_garmin_pagination import A, B, evidence
from test_garmin_pagination import paginated as paginated
from test_garmin_pull import KEY, TODAY, Clock, service
from test_garmin_pull import key_file as key_file
from test_garmin_response_limits import Handler, build

from app.garmin import client as garmin
from app.models.garmin import GarminApiResponse, GarminRawRecord, GarminRecordObservation
from app.models.health import HeartRateVariability
from app.services.providers.garmin_pull import BY_NAME

ERROR = "Garmin response could not be decoded as JSON"
PATH, _ = BY_NAME["hrv"].request(TODAY, TODAY)
DUPLICATES = {
    "duplicate": b'{"totalSteps":7000,"totalSteps":9000}',
    "escaped-duplicate": b'{"totalSteps":7000,"total\\u0053teps":9000}',
    "equal-duplicate": b'{"totalSteps":7000,"totalSteps":7000}',
    "nested-duplicate": b'[{"details":{"totalSteps":7000,"totalSteps":9000}}]',
}


@pytest.fixture(
    params=["syntax", "encoding", "nesting", "escaped-quotes", "quote-recovery", *DUPLICATES]
)
def bad_body(request):
    if request.param in DUPLICATES:
        healthy = json.dumps({**hrv_summary(TODAY), "lastNightAvg": 60}).encode()
        raw = b'{"hrvSummary":%s,"debug":%s,"note":"synthetic-response"}'
        return raw % (healthy, DUPLICATES[request.param]), None
    inner = b'"synthetic-response"'
    return {
        "syntax": (b'{"error":' + inner, json.JSONDecodeError),
        "encoding": (b'["\xff",' + inner + b"]", UnicodeDecodeError),
        "nesting": (b"[" * 10000 + inner + b"]" * 10000, RecursionError),
        "escaped-quotes": (inner + b' "' + b'\\"' * 16384, json.JSONDecodeError),
        "quote-recovery": (b'"' + b'\\"' * 16384 + b"\n" + inner, json.JSONDecodeError),
    }[request.param]


@pytest.mark.parametrize("echo", [False, "direct", "escaped"])
async def test_client_decode_failure_is_safe_and_keeps_complete_capture(key_file, bad_body, echo):
    raw, error_type = bad_body
    if echo:
        raw = raw.replace(b"synthetic-response", f"synthetic-response {KEY} {TOKEN}".encode())
    if echo == "escaped":
        raw = raw.replace(KEY.encode(), b"\\u0073" + KEY[1:].encode())
        raw = raw.replace(TOKEN.encode(), "".join(f"\\u{ord(c):04x}" for c in TOKEN).encode())
    assert len(raw) < min(garmin.MAX_WIRE_BYTES, garmin.MAX_DECODED_BYTES)
    if error_type is None:
        assert json.loads(raw)["hrvSummary"]["lastNightAvg"] == 60
    else:
        with pytest.raises(error_type):
            json.loads(raw)  # Real decoder; no recursion-limit changes or mocked parser.
    handler = Handler(httpx.Response(200, content=raw))
    client, exchanges = build(key_file, handler), []
    try:
        with pytest.raises(garmin.GarminError, match=ERROR) as caught:
            await client.get("/garmin/test", kind="test", exchanges=exchanges)
        assert type(caught.value).__name__ == "GarminResponseDecodeError"
        assert caught.value.__cause__ is None and caught.value.__context__ is None
        assert "synthetic-response" not in "".join(traceback.format_exception(caught.value))
        assert len(handler.requests) == len(exchanges) == 2
        capture = exchanges[-1]
        assert (capture.body, capture.complete, capture.error) == (
            garmin.REDACTED_BODY if echo else raw,
            True,
            None,
        )
        assert capture.size == len(raw) and capture.sha256 == hashlib.sha256(raw).hexdigest()
    finally:
        await client.aclose()


@pytest.mark.parametrize("backfill", [False, True])
@pytest.mark.parametrize("echo", [False, "direct", "escaped"])
async def test_pull_decode_failure_archives_once_and_resumes(
    sessions, key_file, bad_body, backfill, echo, caplog
):
    fake, clock = RetryingGarmin([200]), Clock()
    fake.bodies[PATH] = {"hrvSummary": hrv_summary(TODAY)}
    pull = service(sessions, fake, key_file, clock, garmin_lookback_days=1)
    try:
        assert not (await pull.run(["hrv"])).failed
        async with sessions() as session:
            normalized = (await session.execute(select(HeartRateVariability.__table__))).all()
        assert len(normalized) == 1
        before, success = await evidence(sessions), clock.now.isoformat()
        clock.now += timedelta(minutes=1)
        raw, _ = bad_body
        if echo:
            raw = raw.replace(b"synthetic-response", f"synthetic-response {KEY} {TOKEN}".encode())
        if echo == "escaped":
            raw = raw.replace(KEY.encode(), b"\\u0073" + KEY[1:].encode())
            raw = raw.replace(TOKEN.encode(), "".join(f"\\u{ord(c):04x}" for c in TOKEN).encode())
        fake.statuses[PATH], fake.outcomes = [401, 429, 503], [503, 200]

        def respond(request):
            response = fake(request)
            if request.method == "GET" and request.url.path == PATH and response.status_code == 200:
                return httpx.Response(200, content=raw, headers={"www-authenticate": TOKEN})
            return response

        await pull.client.http.aclose()
        pull.client.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        since = TODAY - timedelta(days=1)
        if backfill:
            result = await pull.backfill(since, TODAY, kinds=["hrv", "steps"])
            entry = result["kinds"]["hrv"]
            assert result["failed"] and not entry["complete"]
        else:
            result = await pull.run(["hrv", "steps"])
            entry = result.kinds["hrv"].as_dict()
            assert result.failed
        after = await evidence(sessions)
        for model, rows in before.items():
            assert after[model][: len(rows)] == rows
        responses = after[GarminApiResponse][len(before[GarminApiResponse]) :]
        assert [(r.method, r.status_code) for r in responses] == [
            ("GET", 200),
            ("GET", 401),
            ("POST", 503),
            ("POST", 200),
            ("GET", 429),
            ("GET", 503),
            ("GET", 200),
            ("GET", 200),
        ]
        failed = responses[-2]
        assert failed.body == (garmin.REDACTED_BODY if echo else raw)
        assert failed.complete and failed.body_size == len(raw) and failed.error is None
        assert failed.body_sha256 == hashlib.sha256(raw).hexdigest()
        assert failed.headers["www-authenticate"] == garmin.REDACTED_HEADER
        assert all(r.body == garmin.REDACTED_BODY for r in responses if r.method == "POST")
        for model in (GarminRawRecord, GarminRecordObservation):
            assert after[model] == before[model]
        async with sessions() as session:
            assert (
                await session.execute(select(HeartRateVariability.__table__))
            ).all() == normalized
        assert entry["error"] == ERROR and entry["records"] == entry["new_records"] == 0
        status = await pull.status()
        state = status["kinds"]["hrv"]
        assert status["failing_kinds"] == ["hrv"] and status["last_error"] == ERROR
        assert state["last_success_at"] == success < state["last_error_at"]
        assert status["kinds"]["steps"]["last_success_at"] == clock.now.isoformat()
        if backfill:
            assert state["backfill"]["next"] == str(TODAY) and not state["backfill"]["complete"]
        logged = str([record.__dict__ for record in caplog.records])
        assert all(value not in logged for value in (KEY, TOKEN, "synthetic-response"))
        clock.now += timedelta(minutes=1)
        await pull.client.http.aclose()
        pull.client.http = httpx.AsyncClient(transport=fake.transport())
        fake.requests.clear()
        if backfill:
            recovered = await pull.backfill(since, TODAY, kinds=["hrv", "steps"])
            assert not recovered["failed"] and recovered["kinds"]["hrv"]["complete"]
            assert recovered["kinds"]["hrv"]["resumed_from"] == str(TODAY)
            assert [r.url.path for r in fake.requests] == [PATH]
        else:
            assert not (await pull.run(["hrv", "steps"])).failed
        status = await pull.status()
        assert status["failing_kinds"] == []
        assert status["kinds"]["hrv"]["last_success_at"] > state["last_error_at"]
        kept = await evidence(sessions)
        for model, rows in after.items():
            assert kept[model][: len(rows)] == rows
        assert kept[GarminRawRecord] == before[GarminRawRecord]
        assert len(kept[GarminRecordObservation]) == len(before[GarminRecordObservation]) + 1
    finally:
        await pull.aclose()


@pytest.mark.parametrize(
    "encoding", ["utf-8", "utf-16", "utf-16-le", "utf-16-be", "utf-32", "utf-32-le", "utf-32-be"]
)
@pytest.mark.parametrize("raw", DUPLICATES.values())
def test_duplicate_keys_in_supported_encodings_have_no_parser_chain(raw, encoding):
    with pytest.raises(garmin.GarminResponseDecodeError, match=f"^{ERROR}$") as caught:
        garmin.decode_response_body(raw.decode().encode(encoding))
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert "totalSteps" not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-32"])
def test_keys_may_repeat_in_separate_objects(encoding):
    raw = b'{"totalSteps":7000,"children":[{"totalSteps":9000},{"total\\u0053teps":7000}]}'
    assert garmin.decode_response_body(raw.decode().encode(encoding)) == json.loads(raw)


@pytest.mark.parametrize("backfill", [False, True])
async def test_later_ambiguous_activity_page_preserves_committed_pages(
    sessions, paginated, backfill
):
    pull, fake, clock = paginated
    raw = b"[" + json.dumps(B[0]).encode()[:-1] + b',"activityId":9000}]'
    fake.bodies[ACTIVITY_PATH] = lambda r: A if r.url.params["start"] == "0" else []
    failing = True

    def respond(request):
        response = fake(request)
        if failing and request.url.path == ACTIVITY_PATH and request.url.params["start"] == "2":
            return httpx.Response(200, content=raw)
        return response

    await pull.client.http.aclose()
    pull.client.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    report = (
        await pull.backfill(TODAY, TODAY, kinds=["activities"])
        if backfill
        else (await pull.run(["activities"])).as_dict()
    )
    entry = report["kinds"]["activities"]
    assert entry["error"] == ERROR and entry["records"] == entry["new_records"] == 2
    kept = await evidence(sessions)
    assert [r.body for r in kept[GarminRawRecord]] == A
    responses = [r for r in kept[GarminApiResponse] if r.kind == "activities"]
    assert len(responses) == 2 and responses[-1].body == raw
    assert (responses[-1].body_size, responses[-1].body_sha256) == (
        len(raw),
        hashlib.sha256(raw).hexdigest(),
    )
    assert len(kept[GarminRecordObservation]) == 2
    assert all(r.response_id == responses[0].id for r in kept[GarminRecordObservation])
    state = (await pull.status())["kinds"]["activities"]
    assert state["last_success_at"] is None
    if backfill:
        assert state["backfill"]["next"] == str(TODAY) and not state["backfill"]["complete"]
    failing = False
    clock.now += timedelta(minutes=1)
    recovered = (
        await pull.backfill(TODAY, TODAY, kinds=["activities"])
        if backfill
        else (await pull.run(["activities"])).as_dict()
    )
    assert recovered["kinds"]["activities"]["error"] is None
    assert (await pull.status())["failing_kinds"] == []
    assert (await pull.status())["kinds"]["activities"]["last_success_at"] > state["last_error_at"]
    if backfill:
        assert recovered["kinds"]["activities"]["complete"]
    after = await evidence(sessions)
    for model, rows in kept.items():
        assert after[model][: len(rows)] == rows
    assert after[GarminRawRecord] == kept[GarminRawRecord]


@pytest.mark.parametrize("failure", [asyncio.CancelledError, TypeError, RuntimeError])
@pytest.mark.parametrize("renew_token", [False, True])
async def test_unrelated_failures_are_not_reclassified(key_file, monkeypatch, failure, renew_token):
    def fail(*_, **__):
        raise failure("synthetic-programming-error")

    client = build(key_file, Handler(httpx.Response(200, content=b"{}")))
    try:
        await client.token([])
        client._token = "previous.token.sig"
        previous = client._token, client._expires_at
        target, name = (garmin, "token_expiry") if renew_token else (garmin.json, "loads")
        monkeypatch.setattr(target, name, fail)
        with pytest.raises(failure, match="synthetic-programming-error"):
            await (client.token([], force=True) if renew_token else client.get(PATH, kind="test"))
        assert (client._token, client._expires_at) == previous
    finally:
        await client.aclose()
