"""The thin Garmin client: key handling, token exchange, pacing, retries and captures."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from garmin_records import TOKEN, FakeGarmin

from app.garmin.client import (
    MAX_ATTEMPTS,
    MIN_INTERVAL,
    TOKEN_PATH,
    GarminAuthError,
    GarminClient,
    GarminKeyError,
    GarminRequestError,
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


def test_token_expiry_reads_exp_or_assumes_a_day():
    assert token_expiry(TOKEN, NOW) == datetime(2100, 1, 1, tzinfo=UTC)
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
    # The token exchange and the GET are both captured; the key is in neither.
    assert [(e.method, e.path, e.status_code) for e in first.exchanges] == [
        ("POST", TOKEN_PATH, 200),
        ("GET", "/x", 200),
    ]
    assert all(KEY not in str(e.row()) and KEY not in e.body.decode() for e in first.exchanges)
    assert first.exchanges[0].body == TOKEN.encode()
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


async def test_429_and_5xx_back_off_and_retry_then_give_up(key_file):
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
    fake.headers["/x"] = {}
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


async def test_token_is_renewed_an_hour_before_expiry(key_file):
    fake = FakeGarmin()
    fake.bodies["/x"] = {}
    clock = Clock()
    client = build(key_file, fake, clock)
    await client.get("/x", kind="test")
    clock.now = datetime(2099, 12, 31, 23, 30, tzinfo=UTC)  # 30 minutes before ``exp``
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
