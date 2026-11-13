"""The thin Garmin client: key handling, token exchange, pacing, retries and captures."""

import asyncio
import hashlib
import traceback
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from unittest.mock import Mock

import httpx
import pytest
from garmin_records import TOKEN, FakeGarmin
from garmin_token_retries import RENEWED_TOKEN, GetTransportGarmin, RetryingGarmin
from garmin_token_retries import bad_retry_after as bad_retry_after
from garmin_token_retries import invalid_token_body as invalid_token_body
from garmin_token_retries import token_variant as token_variant

from app.core.http_retry import retry_after as parse_retry_after
from app.garmin import client as garmin
from app.garmin.client import (
    MAX_ATTEMPTS,
    MIN_INTERVAL,
    REDACTED_BODY,
    TOKEN_PATH,
    GarminAuthError,
    GarminClient,
    GarminKeyError,
    GarminRequestError,
    GarminResponseRefused,
    GarminUnavailableError,
    read_api_key,
    retry_after,
    token_expiry,
)

KEY = "synthetic-personal-api-key-0123456789"
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


@pytest.fixture
def key_file(tmp_path):
    path = tmp_path / "connect-personal-api-key"
    path.write_text(KEY + "\n")
    return path


class Clock:
    def __init__(self) -> None:
        self.now = NOW
        self.slept: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += timedelta(seconds=seconds)


def build(key_file, fake: FakeGarmin, clock: Clock) -> GarminClient:
    return GarminClient(
        key_file,
        http=httpx.AsyncClient(transport=fake.transport()),
        sleep=clock.sleep,
        clock=clock,
    )


def test_api_key_is_read_from_its_file_only(tmp_path, key_file):
    assert read_api_key(key_file) == KEY
    with pytest.raises(GarminKeyError, match="not found"):
        read_api_key(tmp_path / "missing")
    empty = tmp_path / "empty"
    empty.write_text("  \n")
    with pytest.raises(GarminKeyError, match="empty or malformed") as info:
        read_api_key(empty)
    assert KEY not in str(info.value)


def test_token_expiry_reads_exp_or_assumes_a_day(token_variant):
    assert token_expiry(TOKEN, NOW) == datetime(2100, 1, 1, tzinfo=UTC)
    if token_variant != TOKEN:
        assert token_expiry(token_variant, NOW) == NOW + timedelta(hours=24)
    assert token_expiry("not-a-jwt", NOW) == NOW + timedelta(hours=24)


def test_retry_after_accepts_seconds_dates_and_nothing():
    assert retry_after({"retry-after": "7"}) == 7.0
    assert retry_after({}) is None
    assert retry_after({"retry-after": "garbage"}) is None
    assert retry_after({"retry-after": "Thu, 01 Jan 2004 00:00:00 GMT"}) == 0.0


async def test_token_exchange_uses_apikey_header_and_the_token_is_reused(key_file):
    fake = FakeGarmin()
    fake.bodies["/x"] = {"ok": True}
    clock = Clock()
    client = build(key_file, fake, clock)

    first = await client.get("/x", kind="test")
    second = await client.get("/x", kind="test")

    token_request = fake.requests[0]
    assert token_request.url.path == TOKEN_PATH
    assert token_request.headers["authorization"] == f"ApiKey {KEY}"
    assert fake.token_requests == 1
    assert fake.requests[1].headers["authorization"] == f"Bearer {TOKEN}"
    assert first.json == {"ok": True} and second.json == {"ok": True}
    # The token exchange and the GET are both captured; the key is in neither, and the token
    # exchange keeps its digest and size but not the token (tests/test_garmin_token_redaction.py).
    assert [(e.method, e.path, e.status_code) for e in first.exchanges] == [
        ("POST", TOKEN_PATH, 200),
        ("GET", "/x", 200),
    ]
    assert all(KEY not in str(e.row()) and KEY not in e.body.decode() for e in first.exchanges)
    assert first.exchanges[0].body == REDACTED_BODY
    assert TOKEN not in str(first.exchanges[0].row())
    assert first.exchanges[0].sha256 == hashlib.sha256(TOKEN.encode()).hexdigest()
    assert first.exchanges[1].sha256 and first.exchanges[1].request_sha256
    assert len(second.exchanges) == 1  # no new token
    assert clock.slept == [MIN_INTERVAL, MIN_INTERVAL]  # paced between every request
    await client.aclose()


async def test_refused_key_raises_without_the_key_in_the_message(key_file):
    fake = FakeGarmin()
    fake.token_status = 401
    client = build(key_file, fake, Clock())
    with pytest.raises(GarminAuthError, match="HTTP 401") as info:
        await client.get("/x", kind="test")
    assert KEY not in str(info.value)
    await client.aclose()


async def test_missing_key_file_fails_before_any_request(tmp_path):
    fake = FakeGarmin()
    client = build(tmp_path / "missing", fake, Clock())
    with pytest.raises(GarminKeyError):
        await client.get("/x", kind="test")
    assert fake.requests == []
    await client.aclose()


@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("character", ["é", "🙂", *map(chr, range(32)), "\x7f"])
async def test_unsafe_key_is_rejected_before_http(key_file, cached, character, caplog):
    fake = FakeGarmin()
    client, exchanges = build(key_file, fake, Clock()), []
    try:
        if cached:
            await client.token(exchanges)
        key_file.write_text(KEY + character + "-suffix", encoding="utf-8")
        with pytest.raises(GarminKeyError) as caught:
            await client.get("/x", kind="test", exchanges=exchanges)
        error = caught.value
        assert error.__cause__ is None and error.__context__ is None
        assert KEY not in str(error.args) + "".join(traceback.format_exception(error)) + caplog.text
        assert fake.paths() == [TOKEN_PATH] * int(cached)
        assert len(exchanges) == int(cached)
    finally:
        await client.aclose()


@pytest.mark.parametrize("mode", ["initial", "401", "forced"])
async def test_invalid_token_is_not_retried_cached_or_chained(
    key_file, invalid_token_body, mode, caplog
):
    fake, clock = FakeGarmin(), Clock()
    client, exchanges = build(key_file, fake, clock), []
    if mode != "initial":
        await client.token([])
    fake.statuses["/x"] = [401] if mode == "401" else []

    def respond(request):
        if request.method == "POST":
            fake.requests.append(request)
            return httpx.Response(
                200, content=invalid_token_body, headers={b"date": invalid_token_body}
            )
        return fake(request)

    await client.http.aclose()
    client.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        for _ in range(2):  # A later call must exchange again, never reuse the old bearer.
            with pytest.raises(GarminAuthError) as caught:
                if mode == "forced":
                    await client.token(exchanges, force=True)
                else:
                    await client.get("/x", kind="test", exchanges=exchanges)
            error = caught.value
            assert error.__cause__ is None and error.__context__ is None
            diagnostic = str(error.args) + "".join(traceback.format_exception(error)) + caplog.text
            assert all(s not in diagnostic for s in (KEY, TOKEN, "synthetic-invalid-bearer-prefix"))
            assert client._expires_at is None and client._token in (None, TOKEN)
        assert fake.paths() == [TOKEN_PATH] * int(mode != "initial") + (
            ["/x"] if mode == "401" else []
        ) + [TOKEN_PATH, TOKEN_PATH]
        assert all(delay == MIN_INTERVAL for delay in clock.slept)
        for exchange in exchanges[-2:]:
            assert exchange.body == REDACTED_BODY
            assert exchange.headers["date"] == "[redacted]"
            assert (exchange.sha256, exchange.size) == (
                hashlib.sha256(invalid_token_body).hexdigest(),
                len(invalid_token_body),
            )
        fake.token = "synthetic-recovered-bearer"
        await client.http.aclose()
        client.http = httpx.AsyncClient(transport=fake.transport())
        assert (await client.get("/x", kind="test")).json == []
        assert fake.requests[-1].headers["authorization"] == f"Bearer {fake.token}"
    finally:
        await client.aclose()


async def test_printable_ascii_credentials_keep_outer_whitespace_and_expiry_fallback(key_file):
    fake, clock = FakeGarmin(), Clock()
    key = "".join(map(chr, range(33, 127)))
    token = "synthetic-opaque-bearer " + key
    key_file.write_text(" \t\n" + key + "\r\n")
    fake.token = " \t\n" + token + "\r\n"
    client = build(key_file, fake, clock)
    try:
        await client.get("/x", kind="test")
        assert fake.requests[0].headers["authorization"] == f"ApiKey {key}"
        assert fake.requests[1].headers["authorization"] == f"Bearer {token}"
        assert client._expires_at == NOW + timedelta(hours=24)
    finally:
        await client.aclose()


async def test_401_renews_the_token_once_then_fails(key_file):
    fake = FakeGarmin()
    fake.bodies["/x"] = [1]
    fake.statuses["/x"] = [401]
    clock = Clock()
    client = build(key_file, fake, clock)
    response = await client.get("/x", kind="test")
    assert fake.token_requests == 2
    assert [e.status_code for e in response.exchanges] == [200, 401, 200, 200]
    assert response.json == [1]

    fake.statuses["/x"] = [401, 401]
    with pytest.raises(GarminAuthError, match="twice"):
        await client.get("/x", kind="test")
    assert fake.token_requests == 3
    await client.aclose()


async def test_429_and_5xx_back_off_and_retry_then_give_up(key_file, bad_retry_after):
    fake = FakeGarmin()
    fake.bodies["/x"] = {"n": 1}
    fake.statuses["/x"] = [429, 503]
    fake.headers["/x"] = {"retry-after": "5"}
    clock = Clock()
    client = build(key_file, fake, clock)
    response = await client.get("/x", kind="test")
    assert [e.status_code for e in response.exchanges] == [200, 429, 503, 200]
    assert 5.0 in clock.slept  # Retry-After honoured

    fake.statuses["/x"] = [500] * MAX_ATTEMPTS
    fake.headers["/x"] = {"retry-after": bad_retry_after}
    with pytest.raises(GarminUnavailableError):
        await client.get("/x", kind="test")
    assert fake.paths().count("/x") == 3 + MAX_ATTEMPTS

    fake.statuses["/x"] = [429]
    fake.headers["/x"] = {"retry-after": "3600"}  # beyond one request's budget: no retry
    with pytest.raises(GarminUnavailableError):
        await client.get("/x", kind="test")
    await client.aclose()


async def test_400_is_a_request_error_and_is_not_retried(key_file):
    fake = FakeGarmin()
    fake.statuses["/range"] = [400]
    client = build(key_file, fake, Clock())
    with pytest.raises(GarminRequestError) as info:
        await client.get("/range", {"startDate": "2026-01-01"}, kind="steps")
    assert info.value.status_code == 400
    assert fake.paths().count("/range") == 1
    await client.aclose()


@pytest.mark.parametrize("failure", [httpx.ConnectError, httpx.ReadTimeout])
@pytest.mark.parametrize("renew", [False, True])
@pytest.mark.parametrize("recover", [False, True])
async def test_get_transport_errors_drop_credentials_and_preserve_retries(
    key_file, failure, renew, recover, caplog
):
    fake, clock = GetTransportGarmin(), Clock()
    fake.bodies["/x"] = {"ok": True}
    fake.statuses["/x"] = [401] if renew else []
    fake.echo_401.add("/x")
    count = 1 if recover else MAX_ATTEMPTS
    fake.failures["/x"] = [failure] * count
    client, exchanges = build(key_file, fake, clock), []
    try:
        if recover:
            assert (await client.get("/x", kind="test", exchanges=exchanges)).json == {"ok": True}
        else:
            with pytest.raises(GarminUnavailableError, match="Garmin unreachable for /x") as caught:
                await client.get("/x", kind="test", exchanges=exchanges)
            assert caught.value.__cause__ is None and caught.value.__context__ is None
            assert all(
                s not in "".join(traceback.format_exception(caught.value))
                for s in (KEY, TOKEN, RENEWED_TOKEN)
            )
        assert fake.token_requests == 1 + renew
        assert fake.paths().count("/x") == count + renew + recover
        assert [e.status_code for e in exchanges] == [200] + ([401, 200] if renew else []) + (
            [200] if recover else []
        )
        assert clock.slept == [MIN_INTERVAL] * (1 + 2 * renew) + [
            garmin.backoff(i) for i in range(count if recover else count - 1)
        ]
        assert (
            fake.requests[-1].headers["authorization"]
            == f"Bearer {RENEWED_TOKEN if renew else TOKEN}"
        )
        failed = [r for r in caplog.records if r.msg == "GARMIN_REQUEST_FAILED"]
        assert len(failed) == count and all(r.error_type == failure.__name__ for r in failed)
        assert all(
            s not in str([r.__dict__ for r in caplog.records]) for s in (KEY, TOKEN, RENEWED_TOKEN)
        )
    finally:
        await client.aclose()


async def test_token_is_renewed_an_hour_before_expiry(key_file, token_variant):
    fake = FakeGarmin()
    fake.token = token_variant
    fake.bodies["/x"] = {}
    clock = Clock()
    client = build(key_file, fake, clock)
    await client.get("/x", kind="test")
    clock.now = client._expires_at - garmin.TOKEN_RENEWAL_MARGIN - timedelta(seconds=1)
    assert await client.token([]) == token_variant and fake.token_requests == 1
    clock.now += timedelta(seconds=1)
    await client.get("/x", kind="test")
    assert fake.token_requests == 2
    await client.aclose()


async def test_query_parameters_are_sorted_and_kept_without_credentials(key_file):
    fake = FakeGarmin()
    fake.bodies["/x"] = {}
    client = build(key_file, fake, Clock())
    response = await client.get(
        "/x", {"startDate": "2026-01-02", "endDate": "2026-01-03"}, kind="t"
    )
    assert response.exchange.query == "endDate=2026-01-03&startDate=2026-01-02"
    assert "authorization" not in {name.lower() for name in response.exchange.headers}
    await client.aclose()


async def test_204_and_an_empty_body_mean_no_data(key_file):
    fake = FakeGarmin()
    fake.statuses["/hrv-service/hrv/2026-11-07"] = [204]
    fake.bodies["/empty"] = None
    client = build(key_file, fake, Clock())
    none = await client.get("/hrv-service/hrv/2026-11-07", kind="hrv")
    assert none.json is None and none.exchange.status_code == 204
    empty = await client.get("/empty", kind="test")
    assert empty.json is None and empty.exchange.body == b"null"
    await client.aclose()


@pytest.mark.parametrize(
    "outcomes",
    [
        [429, 503, 200],
        [httpx.ConnectError, 200],
        [httpx.ReadTimeout, 200],
        [429, httpx.ConnectError, 503, 200],
    ],
)
async def test_token_transient_failures_recover_and_cache_the_token(
    key_file, outcomes, token_variant
):
    fake, clock = RetryingGarmin(outcomes), Clock()
    fake.token = token_variant
    fake.bodies["/x"] = {"ok": True}
    client = build(key_file, fake, clock)
    response = await client.get("/x", kind="test")
    assert response.json == {"ok": True}
    assert fake.token_requests == len(outcomes)
    assert [(e.attempt, e.status_code) for e in response.exchanges if e.kind == "token"] == [
        (i, status) for i, status in enumerate(outcomes) if isinstance(status, int)
    ]
    assert clock.slept == [garmin.backoff(i) for i in range(len(outcomes) - 1)] + [MIN_INTERVAL]
    assert [e.status_code for e in (await client.get("/x", kind="test")).exchanges] == [200]
    assert fake.token_requests == len(outcomes)
    await client.aclose()


@pytest.mark.parametrize(
    "outcomes",
    [
        [status] * MAX_ATTEMPTS
        for status in (429, 500, 503, 599, httpx.ConnectError, httpx.ReadTimeout)
    ]
    + [[429, httpx.ConnectError, 503, httpx.ReadTimeout, 500]],
)
async def test_token_retry_budget_is_bounded_and_errors_are_safe(
    key_file, outcomes, caplog, bad_retry_after
):
    fake, clock = RetryingGarmin([*outcomes, 200]), Clock()
    fake.token_headers["retry-after"] = bad_retry_after
    client, exchanges = build(key_file, fake, clock), []
    with pytest.raises(GarminUnavailableError, match="token endpoint") as info:
        await client.get("/x", kind="test", exchanges=exchanges)
    assert fake.paths() == [TOKEN_PATH] * MAX_ATTEMPTS
    assert [e.attempt for e in exchanges] == [
        i for i, outcome in enumerate(outcomes) if isinstance(outcome, int)
    ]
    assert clock.slept == [garmin.backoff(i) for i in range(MAX_ATTEMPTS - 1)]
    assert info.value.__cause__ is None
    assert KEY not in str(info.value) + caplog.text and TOKEN not in str(info.value) + caplog.text
    await client.aclose()


async def test_failed_renewal_does_not_reuse_the_revoked_cached_token(key_file):
    fake = RetryingGarmin([200, *([503] * MAX_ATTEMPTS), 200])
    fake.statuses["/x"] = [401]
    client, exchanges = build(key_file, fake, Clock()), []
    with pytest.raises(GarminUnavailableError):
        await client.get("/x", kind="test", exchanges=exchanges)
    assert fake.paths().count("/x") == 1
    response = await client.get("/x", kind="test")
    assert [(e.method, e.attempt, e.status_code) for e in response.exchanges] == [
        ("POST", 0, 200),
        ("GET", 0, 200),
    ]
    assert fake.token_requests == MAX_ATTEMPTS + 2
    assert [e.status_code for e in exchanges] == [200, 401, *([503] * MAX_ATTEMPTS)]
    await client.aclose()


@pytest.mark.parametrize("delay", [0, 7, garmin.MAX_RETRY_AFTER, garmin.MAX_RETRY_AFTER + 1])
@pytest.mark.parametrize("as_date", [False, True])
@pytest.mark.parametrize("post", [False, True])
@pytest.mark.parametrize("status", [429, 503])
async def test_retry_after_honors_the_minimum_and_never_retries_early(
    key_file, delay, as_date, post, status
):
    fake, clock = RetryingGarmin([status, 200] if post else [200]), Clock()
    fake.statuses["/x"] = [] if post else [status, 200]
    client, exchanges = build(key_file, fake, clock), []
    if not post:
        await client.token([])
        clock.now += timedelta(seconds=MIN_INTERVAL)
    header = (
        format_datetime(clock.now + timedelta(seconds=delay), usegmt=True)
        if as_date
        else str(delay)
    )
    fake.token_headers["retry-after"] = header
    fake.headers["/x"] = {"retry-after": header}
    if delay > garmin.MAX_RETRY_AFTER:
        with pytest.raises(GarminUnavailableError):
            await client.get("/x", kind="test", exchanges=exchanges)
        assert len(exchanges) == 1 and clock.slept == []
    else:
        await client.get("/x", kind="test", exchanges=exchanges)
        elapsed = (exchanges[1].fetched_at - exchanges[0].fetched_at).total_seconds()
        assert elapsed == max(delay, MIN_INTERVAL)
        assert clock.slept == ([0, MIN_INTERVAL] if delay == 0 else [delay]) + (
            [MIN_INTERVAL] if post else []
        )
    await client.aclose()


@pytest.mark.parametrize(
    ("status", "oversized"),
    [(status, True) for status in (200, 301, 400, 401, 403, 404, 429, 503)]
    + [(status, False) for status in (301, 400, 401, 403, 404)],
)
async def test_permanent_or_refused_token_answers_are_not_retried(
    key_file, status, oversized, monkeypatch
):
    fake, clock = RetryingGarmin([status, 200]), Clock()
    if oversized:
        monkeypatch.setattr(garmin, "MAX_TOKEN_BYTES", 8)
    client, exchanges = build(key_file, fake, clock), []
    with pytest.raises(GarminResponseRefused if oversized else GarminAuthError):
        await client.get("/x", kind="test", exchanges=exchanges)
    assert fake.token_requests == 1 and len(exchanges) == 1 and not clock.slept
    assert exchanges[0].body == REDACTED_BODY
    assert exchanges[0].complete is not oversized
    await client.aclose()


@pytest.mark.parametrize("final_status", [200, 401])
async def test_transient_token_renewal_does_not_reset_the_one_401_limit(key_file, final_status):
    fake = RetryingGarmin([200, 429, 503, 200])
    fake.statuses["/x"] = [401, final_status]
    client, exchanges = build(key_file, fake, Clock()), []
    if final_status == 401:
        with pytest.raises(GarminAuthError, match="twice"):
            await client.get("/x", kind="test", exchanges=exchanges)
    else:
        await client.get("/x", kind="test", exchanges=exchanges)
    assert [(e.method, e.attempt, e.status_code) for e in exchanges] == [
        ("POST", 0, 200),
        ("GET", 0, 401),
        ("POST", 0, 429),
        ("POST", 1, 503),
        ("POST", 2, 200),
        ("GET", 0, final_status),
    ]
    assert fake.token_requests == 4
    await client.aclose()


@pytest.mark.parametrize("failure", [OSError, OverflowError, ValueError])
def test_platform_timestamp_failure_assumes_a_day(monkeypatch, failure):
    monkeypatch.setattr(garmin, "datetime", Mock(fromtimestamp=Mock(side_effect=failure)))
    assert token_expiry(TOKEN, NOW) == NOW + timedelta(hours=24)


@pytest.mark.parametrize("post", [False, True])
async def test_bad_retry_after_uses_backoff_for_token_and_data(key_file, bad_retry_after, post):
    assert retry_after({"retry-after": bad_retry_after}) is None
    fake, clock = RetryingGarmin([429, 503, 200] if post else [200]), Clock()
    fake.statuses["/x"] = [] if post else [429, 503, 200]
    fake.token_headers["retry-after"] = bad_retry_after
    fake.headers["/x"] = {"retry-after": bad_retry_after}
    client = build(key_file, fake, clock)
    try:
        response = await client.get("/x", kind="test")
        method = "POST" if post else "GET"
        assert [e.status_code for e in response.exchanges if e.method == method] == [429, 503, 200]
        delays = [garmin.backoff(0), garmin.backoff(1)]
        assert clock.slept == (delays + [MIN_INTERVAL] if post else [MIN_INTERVAL] + delays)
    finally:
        await client.aclose()


@pytest.mark.parametrize("failure", [asyncio.CancelledError, TypeError, RuntimeError])
def test_retry_after_does_not_swallow_clock_errors(failure):
    with pytest.raises(failure):
        parse_retry_after(
            "Thu, 12 Nov 2026 12:00:00 GMT", now=Mock(side_effect=failure), strict=False
        )
