"""The Garmin bearer token (and the API key) never reach ``garmin_api_responses``.

The token exchange is archived like every other exchange (status, time, SHA-256 and size of
what Garmin sent), but its body is a fixed placeholder and no kept header carries a credential.
The tests read every column of every archived row, as bytes and as JSON.
"""

import hashlib
import json
import traceback
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from garmin_records import TOKEN, daily_steps
from garmin_token_leaks import SecretEchoGarmin, archived, leaks, scan
from garmin_token_retries import RENEWED_TOKEN, GetTransportGarmin, RetryingGarmin
from garmin_token_retries import bad_retry_after as bad_retry_after
from garmin_token_retries import invalid_token_body as invalid_token_body
from garmin_token_retries import token_variant as token_variant
from sqlalchemy import select
from support.garmin import unlocked

from app.core.config import Settings
from app.garmin.client import (
    KEPT_HEADERS,
    MAX_ATTEMPTS,
    TOKEN_PATH,
    GarminAuthError,
    GarminClient,
    GarminCredentialEchoError,
    GarminKeyError,
    GarminRequestError,
    GarminUnavailableError,
)
from app.models.garmin import GarminPullState, GarminRawRecord

KEY = "synthetic-personal-api-key-0123456789"
PLACEHOLDER = b"[redacted: garmin bearer token]"
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
TODAY = date(2026, 11, 7)
DAYS = [TODAY - timedelta(days=offset) for offset in range(3, -1, -1)]
STEPS = f"/usersummary-service/stats/steps/daily/{DAYS[0].isoformat()}/{DAYS[-1].isoformat()}"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def key_file(tmp_path):
    path = tmp_path / "connect-personal-api-key"
    path.write_text(KEY + "\n")
    return path


@pytest.fixture
def fake():
    fake = SecretEchoGarmin()
    fake.bodies[STEPS] = [daily_steps(day) for day in DAYS]
    return fake


def client(key_file, fake, clock=None) -> GarminClient:
    clock = clock or Clock()
    return GarminClient(
        key_file, http=httpx.AsyncClient(transport=fake.transport()), sleep=clock.sleep, clock=clock
    )


def pull(sessions, fake, key_file):
    clock = Clock()
    settings = Settings(_env_file=None, garmin_enabled=True, garmin_api_key_file=key_file)
    return unlocked(settings, sessions, client=client(key_file, fake, clock), clock=clock)


@pytest.fixture(params=[TOKEN, KEY], ids=["bearer", "api-key"])
def credential(request):
    return request.param


@pytest.fixture(
    params=[(mode, secret) for mode in ("direct", "unicode", "mixed") for secret in (TOKEN, KEY)]
)
def echo_body(request):
    mode, credential = request.param
    escaped = "".join(
        f"\\u{ord(char):04x}" if mode == "unicode" or index % 2 else char
        for index, char in enumerate(credential)
    )

    def body(location):
        value = f"prefix {credential} suffix"
        debug = {value: [None, False, 42]} if location == "key" else {"value": value}
        record = {**daily_steps(TODAY, steps=9999), "debug_token": [{"nested": debug}]}
        raw = json.dumps([record]).encode()
        return raw if mode == "direct" else raw.replace(credential.encode(), escaped.encode())

    return body


@pytest.mark.parametrize("location", ["key", "value"])
@pytest.mark.parametrize("padding", [0, 1024 * 1024])
async def test_successful_get_refuses_credential_echo_without_retry(
    key_file, fake, echo_body, location, padding, caplog
):
    raw = b" " * padding + echo_body(location)

    def respond(request):
        if request.url.path == "/x":
            fake.requests.append(request)
            return httpx.Response(200, content=raw)
        return fake(request)

    garmin, exchanges = client(key_file, fake), []
    await garmin.http.aclose()
    garmin.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(GarminCredentialEchoError) as caught:
            await garmin.get("/x", kind="steps", exchanges=exchanges)
        assert str(caught.value) == "Garmin response contained a credential"
        assert caught.value.__cause__ is None and caught.value.__context__ is None
        assert scan([vars(caught.value)], TOKEN, KEY) == []
        diagnostic = "".join(traceback.format_exception(caught.value)) + caplog.text
        assert all(secret not in diagnostic for secret in (TOKEN, KEY))
        assert fake.paths() == [TOKEN_PATH, "/x"]
        assert len(exchanges) == 2 and exchanges[-1].body == PLACEHOLDER
        assert (exchanges[-1].sha256, exchanges[-1].size) == (sha(raw), len(raw))
        assert scan([exchange.row() for exchange in exchanges], TOKEN, KEY) == []
    finally:
        await garmin.aclose()


@pytest.mark.parametrize(
    "change,content",
    [
        ("rotate", b"synthetic-replacement-api-key-9876543210"),
        ("missing", None),
        ("empty", b""),
        ("malformed", KEY.encode() + b" bad"),
        ("encoding", KEY.encode() + b"\xff"),
    ],
)
async def test_cached_bearer_rereads_key_file_without_retaining_key(
    key_file, fake, change, content, caplog
):
    garmin, exchanges = client(key_file, fake), []
    await garmin.token(exchanges)
    key = "synthetic-replacement-api-key-9876543210"
    if content is None:
        key_file.unlink()
    else:
        key_file.write_bytes(content)
    try:
        if change == "rotate":
            fake.headers["/x"], fake.statuses["/x"] = {"www-authenticate": key}, [400]
            with pytest.raises(GarminRequestError):
                await garmin.get("/x", kind="steps", exchanges=exchanges)
            assert exchanges[-1].headers["www-authenticate"] == "[redacted]"
            fake.bodies["/x"] = {"echo": key}
            with pytest.raises(GarminCredentialEchoError):
                await garmin.get("/x", kind="steps", exchanges=exchanges)
            assert exchanges[-1].body == PLACEHOLDER
        else:
            with pytest.raises(GarminKeyError) as caught:
                await garmin.get("/x", kind="steps", exchanges=exchanges)
            assert caught.value.__cause__ is None and caught.value.__context__ is None
            assert scan([{"trace": traceback.format_exception(caught.value)}], KEY) == []
            assert fake.paths() == [TOKEN_PATH] and len(exchanges) == 1
        assert fake.token_requests == 1
        rows = [vars(garmin), *[e.row() for e in exchanges], *[vars(r) for r in caplog.records]]
        assert scan(rows, KEY, key) == []
    finally:
        await garmin.aclose()


# --- the client's captures ------------------------------------------------------------------


async def test_token_exchange_keeps_status_digest_and_size_but_not_the_token(key_file, fake):
    garmin = client(key_file, fake)
    fake.bodies["/x"] = {"ok": True}
    response = await garmin.get("/x", kind="test")
    token, data = response.exchanges
    assert (token.kind, token.method, token.path, token.status_code) == (
        "token",
        "POST",
        TOKEN_PATH,
        200,
    )
    row = token.row()
    assert row["body"] == PLACEHOLDER
    # The evidence is over the bytes Garmin sent, not over the placeholder.
    assert row["body_sha256"] == sha(TOKEN.encode())
    assert row["body_size"] == len(TOKEN.encode())
    assert scan([row, data.row()], TOKEN, KEY) == []
    # The request's ApiKey header went out but is in no capture.
    assert fake.requests[0].headers["authorization"] == f"ApiKey {KEY}"
    await garmin.aclose()


async def test_credential_headers_on_the_token_answer_are_not_kept(key_file, fake):
    fake.token_headers = {
        "authorization": f"Bearer {TOKEN}",
        "set-cookie": f"session={TOKEN}; HttpOnly",
        "www-authenticate": f'Bearer token="{TOKEN}"',
    }
    fake.bodies["/x"] = {}
    garmin = client(key_file, fake)
    response = await garmin.get("/x", kind="test")
    rows = [exchange.row() for exchange in response.exchanges]
    assert scan(rows, TOKEN, KEY) == []
    assert rows[0]["headers"]["content-type"] == "text/plain"  # harmless headers stay
    assert not {"authorization", "set-cookie"} & set(rows[0]["headers"])
    await garmin.aclose()


async def test_refused_token_exchange_keeps_no_echoed_secret(key_file, fake):
    body = json.dumps({"error": "invalid key", "key": KEY, "token": TOKEN}).encode()
    fake.token_status = 401
    fake.token_error_body = body
    garmin = client(key_file, fake)
    exchanges = []
    with pytest.raises(GarminAuthError, match="HTTP 401"):
        await garmin.get("/x", kind="test", exchanges=exchanges)
    (row,) = [exchange.row() for exchange in exchanges]
    assert row["status_code"] == 401 and row["kind"] == "token"
    assert row["body_sha256"] == sha(body) and row["body_size"] == len(body)
    assert scan([row], TOKEN, KEY) == []
    await garmin.aclose()


@pytest.mark.parametrize("location", ["key", "value"])
@pytest.mark.parametrize(
    "statuses", [[400], [401, 200], [401, 401], [429, 503, 200], [503] * MAX_ATTEMPTS]
)
async def test_get_error_echo_preserves_status_retries_and_masks_headers(
    key_file, fake, echo_body, statuses, location, caplog
):
    garmin, exchanges, pending = client(key_file, fake), [], list(statuses)
    await garmin.token(exchanges)  # Every first GET uses an already cached bearer.
    raw = echo_body(location)

    def respond(request):
        if request.method == "POST":
            return fake(request)
        status = pending.pop(0)
        return httpx.Response(
            status,
            content=b"{}" if status == 200 else raw,
            headers={name: KEY for name in KEPT_HEADERS},
        )

    await garmin.http.aclose()
    garmin.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        error = {400: GarminRequestError, 401: GarminAuthError, 503: GarminUnavailableError}.get(
            statuses[-1]
        )
        if error:
            with pytest.raises(error):
                await garmin.get("/x", kind="steps", exchanges=exchanges)
        else:
            assert (await garmin.get("/x", kind="steps", exchanges=exchanges)).json == {}
        gets = [e for e in exchanges if e.method == "GET"]
        assert [e.status_code for e in gets] == statuses and not pending
        assert fake.token_requests == 1 + int(401 in statuses)
        for exchange in gets:
            body = b"{}" if exchange.status_code == 200 else raw
            assert exchange.body == (body if exchange.status_code == 200 else PLACEHOLDER)
            assert (exchange.sha256, exchange.size) == (sha(body), len(body))
            assert set(exchange.headers.values()) == {"[redacted]"}
        assert (
            scan([e.row() for e in exchanges] + [vars(r) for r in caplog.records], TOKEN, KEY) == []
        )
    finally:
        await garmin.aclose()


# --- the archive (SQLite; tests/integration repeats this on PostgreSQL) ---------------------


async def test_pull_archive_holds_no_token_in_any_column(sessions, fake, key_file):
    fake.token_headers = {"set-cookie": f"session={TOKEN}"}
    service = pull(sessions, fake, key_file)
    summary = await service.run(["steps"])
    assert not summary.failed, summary.as_dict()
    rows = await archived(sessions)
    assert [(row["kind"], row["status_code"]) for row in rows] == [("token", 200), ("steps", 200)]
    assert scan(rows, TOKEN, KEY) == []
    token_row, steps_row = rows
    assert bytes(token_row["body"]) == PLACEHOLDER
    assert token_row["body_sha256"] == sha(TOKEN.encode())
    assert token_row["body_size"] == len(TOKEN.encode())
    assert token_row["fetched_at"] is not None
    # The health data response is untouched: its stored bytes match its digest and size.
    body = bytes(steps_row["body"])
    assert json.loads(body) == [daily_steps(day) for day in DAYS]
    assert steps_row["body_sha256"] == sha(body) and steps_row["body_size"] == len(body)
    await service.aclose()


async def test_retry_renewal_and_failure_after_the_token_keep_it_out(sessions, fake, key_file):
    fake.statuses[STEPS] = [503, 401, 400]
    fake.echo_401 = {STEPS}
    service = pull(sessions, fake, key_file)
    summary = await service.run(["steps"])
    assert summary.kinds["steps"].error.startswith("Garmin answered HTTP 400")
    rows = await archived(sessions)
    assert [(row["kind"], row["status_code"]) for row in rows] == [
        ("token", 200),
        ("steps", 503),
        ("steps", 401),
        ("token", 200),
        ("steps", 400),
    ]
    assert await leaks(sessions, TOKEN, KEY) == []
    assert all(bytes(row["body"]) == PLACEHOLDER for row in rows if row["kind"] == "token")
    await service.aclose()


async def test_refused_key_during_a_pull_archives_the_refusal_without_secrets(
    sessions, fake, key_file
):
    fake.token_status = 403
    fake.token_error_body = f"key {KEY} refused; last token {TOKEN}".encode()
    service = pull(sessions, fake, key_file)
    summary = await service.run(["steps"])
    assert summary.failed
    rows = await archived(sessions)
    assert [(row["kind"], row["status_code"]) for row in rows] == [("token", 403)]
    assert await leaks(sessions, TOKEN, KEY) == []
    await service.aclose()


@pytest.mark.parametrize("backfill", [False, True])
@pytest.mark.parametrize("renew", [False, True])
async def test_invalid_token_archives_masked_evidence_and_preserves_failed_progress(
    sessions, fake, key_file, invalid_token_body, backfill, renew, caplog
):
    service = pull(sessions, fake, key_file)
    since = TODAY - timedelta(days=28)
    path = f"/usersummary-service/stats/steps/daily/{TODAY}/{TODAY}" if backfill else STEPS
    fake.bodies[path] = [daily_steps(TODAY)]
    if renew:
        assert not (await service.run(["steps"])).failed
        fake.statuses[path] = [401]
        fake.echo_401.add(path)
    before, previous = await archived(sessions), await service.status()

    def respond(request):
        if request.method == "POST":
            fake.requests.append(request)
            headers = {b"date": invalid_token_body, b"www-authenticate": KEY.encode()}
            return httpx.Response(200, content=invalid_token_body, headers=headers)
        return fake(request)

    await service.client.http.aclose()
    service.client.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        if backfill:
            result = await service.backfill(since, TODAY, kinds=["steps"])
            assert result["failed"]
        else:
            summary = await service.run(["steps"])
            assert summary.failed
            result = summary.as_dict()
        error = result["kinds"]["steps"]["error"]
        rows, status = await archived(sessions), await service.status()
        assert rows[: len(before)] == before
        new = rows[len(before) :]
        assert [(r["method"], r["status_code"]) for r in new] == (
            ([("GET", 200)] if backfill and renew else [])
            + ([("GET", 401)] if renew else [])
            + [("POST", 200)]
        )
        token = new[-1]
        assert bytes(token["body"]) == PLACEHOLDER and token["complete"]
        assert (token["body_sha256"], token["body_size"]) == (
            sha(invalid_token_body),
            len(invalid_token_body),
        )
        assert status["last_success_at"] == previous["last_success_at"]
        assert status["last_error"] == error and status["failing_kinds"] == ["steps"]
        async with sessions() as session:
            state = await session.get(GarminPullState, "steps")
            assert state.last_error == error and state.last_error_at is not None
            if backfill:
                assert state.backfill_next == (TODAY if renew else since)
        assert (
            scan(
                rows + [result, status] + [vars(r) for r in caplog.records],
                KEY,
                TOKEN,
                "synthetic-invalid-bearer-prefix",
            )
            == []
        )
    finally:
        await service.aclose()


async def test_client_side_request_error_message_carries_no_secret(key_file, fake):
    fake.statuses["/range"] = [400]
    garmin = client(key_file, fake)
    with pytest.raises(GarminRequestError) as info:
        await garmin.get("/range", {"startDate": "2026-01-01"}, kind="steps")
    assert TOKEN not in str(info.value) and KEY not in str(info.value)
    await garmin.aclose()


@pytest.mark.parametrize("backfill", [False, True])
@pytest.mark.parametrize("renew", [False, True])
@pytest.mark.parametrize("failure", [httpx.ConnectError, httpx.ReadTimeout])
async def test_get_transport_failure_keeps_evidence_and_failed_window_retryable(
    sessions, key_file, backfill, renew, failure, caplog
):
    fake = GetTransportGarmin()
    fake.bodies[STEPS] = [daily_steps(day) for day in DAYS]
    service = pull(sessions, fake, key_file)
    try:
        assert not (await service.run(["steps"])).failed
        before, success = await archived(sessions), await service.status()
        service.client.clock.now += timedelta(minutes=1)
        path = f"/usersummary-service/stats/steps/daily/{TODAY}/{TODAY}" if backfill else STEPS
        fake.bodies[path] = [daily_steps(TODAY)]
        fake.statuses[path] = [503, 401] if renew else [503]
        fake.echo_401.add(path)
        fake.failures[path] = [failure] * (MAX_ATTEMPTS if renew else MAX_ATTEMPTS - 1)
        since = TODAY - timedelta(days=28)
        if backfill:
            result = await service.backfill(since, TODAY, kinds=["steps"])
            assert result["failed"]
        else:
            summary = await service.run(["steps"])
            assert summary.failed
            result = summary.as_dict()
        error = f"Garmin unreachable for {path}"
        assert result["kinds"]["steps"]["error"] == error
        rows = await archived(sessions)
        assert rows[: len(before)] == before
        new = rows[len(before) :]
        assert [(r["method"], r["status_code"]) for r in new] == (
            ([("GET", 200)] if backfill else [])
            + [("GET", 503)]
            + ([("GET", 401), ("POST", 200)] if renew else [])
        )
        if renew:
            echoed = json.dumps(
                {"error": "invalid_token", "token": TOKEN}, separators=(",", ":")
            ).encode()
            assert bytes(new[-2]["body"]) == PLACEHOLDER
            assert (new[-2]["body_sha256"], new[-2]["body_size"]) == (sha(echoed), len(echoed))
            body = RENEWED_TOKEN.encode()
            assert (new[-1]["body_sha256"], new[-1]["body_size"]) == (sha(body), len(body))
        status = await service.status()
        assert status["last_success_at"] == success["last_success_at"]
        assert status["last_error"] == error and status["failing_kinds"] == ["steps"]
        async with sessions() as session:
            state = await session.get(GarminPullState, "steps")
            assert state.last_error == error and state.last_error_at > state.last_success_at
            assert len((await session.scalars(select(GarminRawRecord))).all()) == len(DAYS)
            assert scan([state.__dict__, result, status], KEY, TOKEN, RENEWED_TOKEN) == []
            if backfill:
                assert state.backfill_next == TODAY and not result["kinds"]["steps"]["complete"]
        assert scan(rows, KEY, TOKEN, RENEWED_TOKEN) == []
        assert all(
            s not in str([r.__dict__ for r in caplog.records]) for s in (KEY, TOKEN, RENEWED_TOKEN)
        )
        service.client.clock.now += timedelta(minutes=1)
        fake.requests.clear()
        if backfill:
            resumed = await service.backfill(since, TODAY, kinds=["steps"])
            assert not resumed["failed"] and resumed["kinds"]["steps"]["complete"]
            assert resumed["kinds"]["steps"]["resumed_from"] == str(TODAY)
        else:
            assert not (await service.run(["steps"])).failed
        assert fake.paths() == [path]
        assert (await service.status())["last_success_at"] > status["last_error_at"]
        kept = await archived(sessions)
        assert kept[: len(rows)] == rows and scan(kept, KEY, TOKEN, RENEWED_TOKEN) == []
    finally:
        await service.aclose()


@pytest.mark.parametrize("outcomes", [[200], [400], [401], [403], [429, 503, 200]])
async def test_initial_token_answers_mask_secrets_and_evidence_the_original_bytes(
    key_file, outcomes
):
    fake = RetryingGarmin(outcomes)
    fake.token_error_body += f"; key {KEY}".encode()
    fake.token_headers = {"www-authenticate": KEY, "date": KEY}
    garmin, exchanges = client(key_file, fake), []
    if outcomes[0] in (400, 401, 403):
        with pytest.raises(GarminAuthError):
            await garmin.get("/x", kind="test", exchanges=exchanges)
    else:
        await garmin.get("/x", kind="test", exchanges=exchanges)
    tokens = [e.row() for e in exchanges if e.kind == "token"]
    assert [r["status_code"] for r in tokens] == outcomes
    for row in tokens:
        body = TOKEN.encode() if row["status_code"] == 200 else fake.token_error_body
        assert row["body"] == PLACEHOLDER
        assert (row["body_sha256"], row["body_size"]) == (sha(body), len(body))
        assert row["headers"]["www-authenticate"] == row["headers"]["date"] == "[redacted]"
    assert scan([e.row() for e in exchanges], TOKEN, KEY) == []
    await garmin.aclose()


@pytest.mark.parametrize("backfill", [False, True])
@pytest.mark.parametrize("recover", [False, True])
@pytest.mark.parametrize("transient", [429, 503, httpx.ConnectError])
async def test_renewal_retries_archive_once_and_preserve_failed_progress(
    sessions, key_file, backfill, recover, transient, caplog, token_variant, bad_retry_after
):
    fake, clock = RetryingGarmin([200]), Clock()
    fake.token = token_variant
    fake.token_error_body = fake.token_error_body.replace(TOKEN.encode(), token_variant.encode())
    fake.token_headers["www-authenticate"] = token_variant
    fake.bodies[STEPS] = [daily_steps(day) for day in DAYS]
    settings = Settings(_env_file=None, garmin_enabled=True, garmin_api_key_file=key_file)
    service = unlocked(settings, sessions, client=client(key_file, fake, clock), clock=clock)
    assert not (await service.run(["steps"])).failed
    before, status_before = await archived(sessions), await service.status()
    assert [(r["kind"], r["status_code"]) for r in before] == [("token", 200), ("steps", 200)]
    assert bytes(before[0]["body"]) == PLACEHOLDER
    assert before[0]["body_sha256"] == sha(token_variant.encode())
    clock.now += timedelta(minutes=1)
    path = f"/usersummary-service/stats/steps/daily/{TODAY}/{TODAY}" if backfill else STEPS
    fake.bodies[path] = [daily_steps(TODAY)] if backfill else fake.bodies[STEPS]
    fake.statuses[path] = [503, 401]
    fake.headers[path] = {"retry-after": bad_retry_after}
    fake.token_headers["retry-after"] = bad_retry_after
    fake.echo_401.add(path)
    fake.outcomes = [transient, 200] if recover else [transient] * MAX_ATTEMPTS
    fake.token_error_body += f"; key {KEY}".encode()
    fake.token_headers["date"] = KEY
    since = TODAY - timedelta(days=28)
    if backfill:
        report = await service.backfill(since, TODAY, kinds=["steps"])
        assert report["failed"] is not recover
        assert report["kinds"]["steps"]["complete"] is recover
    else:
        assert (await service.run(["steps"])).failed is not recover
    rows = await archived(sessions)
    assert rows[: len(before)] == before  # Earlier evidence is immutable.
    new = rows[len(before) :]
    expected = ([("steps", 0, 200)] if backfill else []) + [("steps", 0, 503), ("steps", 1, 401)]
    if isinstance(transient, int):
        expected += [("token", i, transient) for i in range(1 if recover else MAX_ATTEMPTS)]
    if recover:
        expected += [("token", 1, 200), ("steps", 0, 200)]
    assert [(r["kind"], r["attempt"], r["status_code"]) for r in new] == expected
    assert fake.token_requests == 1 + (2 if recover else MAX_ATTEMPTS)
    assert scan(rows, token_variant, TOKEN, KEY) == []
    assert token_variant not in caplog.text and TOKEN not in caplog.text and KEY not in caplog.text
    for row in new:
        if row["kind"] == "token":
            body = token_variant.encode() if row["status_code"] == 200 else fake.token_error_body
            assert bytes(row["body"]) == PLACEHOLDER
            assert (row["body_sha256"], row["body_size"]) == (sha(body), len(body))
            headers = (
                json.loads(row["headers"]) if isinstance(row["headers"], str) else row["headers"]
            )
            assert headers["www-authenticate"] == headers["date"] == "[redacted]"
    status_after = await service.status()
    if recover:
        assert status_after["last_success_at"] != status_before["last_success_at"]
    else:
        assert status_after["last_success_at"] == status_before["last_success_at"]
        assert status_after["last_error"] == "Garmin token endpoint temporarily unavailable"
    async with sessions() as session:
        state = await session.get(GarminPullState, "steps")
        if backfill:
            assert state.backfill_next == (None if recover else TODAY)
        assert len((await session.scalars(select(GarminRawRecord))).all()) == len(DAYS)
    if not recover:
        clock.now += timedelta(minutes=1)
        fake.outcomes, fake.statuses[path] = [200], []
        if backfill:
            resumed = await service.backfill(since, TODAY, kinds=["steps"])
            assert not resumed["failed"] and resumed["kinds"]["steps"]["complete"]
            assert resumed["kinds"]["steps"]["resumed_from"] == str(TODAY)
        else:
            assert not (await service.run(["steps"])).failed
        assert (await service.status())["last_success_at"] > status_after["last_error_at"]
        kept = await archived(sessions)
        assert kept[: len(rows)] == rows and scan(kept, token_variant, TOKEN, KEY) == []
    await service.aclose()
