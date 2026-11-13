"""A Garmin response is read with a budget: a bomb cannot reach memory or the archive."""

import asyncio
import gzip
import hashlib
import json
import tracemalloc
import zlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

import httpx
import pytest
from compressed_records import compressed
from garmin_records import TOKEN, FakeGarmin, daily_steps
from garmin_token_leaks import archived, scan
from http_close_records import ClosingStream, dispose, repeated_cancel, safe_chain
from sqlalchemy import select
from test_garmin_pull import TODAY, Clock, service

from app.core import bounded_http
from app.garmin import client as garmin
from app.garmin.client import TOKEN_KIND as TOKEN_EXCHANGE
from app.garmin.client import (
    GarminClient,
    GarminResponseDecodeError,
    GarminResponseRefused,
    GarminUnavailableError,
)
from app.models.garmin import GarminApiResponse, GarminRawRecord

KEY = "synthetic-personal-api-key"
PATH = "/garmin/test"


class ClosingGarmin(FakeGarmin):
    def __init__(self, method="GET", *, recover=False, **stream_options):
        super().__init__()
        self.method, self.recover, self.options = method, recover, stream_options
        self.fail, self.path, self.streams = True, PATH, []
        self.started = asyncio.Event()

    def __call__(self, request):
        if (
            self.fail
            and request.method == self.method
            and (self.method == "POST" or request.url.path == self.path)
        ):
            for stream in self.streams:
                stream.assert_settled()  # A retry must never overlap its predecessor's cleanup.
            self.requests.append(request)
            self.token_requests += int(request.method == "POST")
            wire = (
                TOKEN.encode() if self.method == "POST" else f'{{"echo":"{TOKEN} {KEY}"}}'.encode()
            )
            stream = ClosingStream(wire, **self.options)
            self.streams.append(stream)
            self.started.set()
            self.fail = not self.recover
            return httpx.Response(200, stream=stream)
        return super().__call__(request)


@pytest.fixture
def key_file(tmp_path):
    path = tmp_path / "key"
    path.write_text(KEY)
    return path


class Handler(FakeGarmin):
    """FakeGarmin with one fully scripted response for ``PATH``."""

    def __init__(self, response):
        super().__init__()
        self.response = response

    def __call__(self, request):
        if request.url.path == PATH:
            self.requests.append(request)
            return self.response(request) if callable(self.response) else self.response
        return super().__call__(request)


def build(key_file, handler):
    async def no_sleep(_):
        return None

    return GarminClient(
        key_file, http=httpx.AsyncClient(transport=handler.transport()), sleep=no_sleep
    )


def gzipped(payload: bytes, **headers) -> httpx.Response:
    return httpx.Response(
        200,
        content=gzip.compress(payload, compresslevel=1),
        headers={"content-encoding": "gzip", "content-type": "application/json", **headers},
    )


@pytest.mark.parametrize("members", [False, True])
async def test_a_normal_compressed_body_is_archived_decoded_as_before(key_file, members):
    body = json.dumps({"steps": 1234, "values": list(range(500))}).encode()
    wire = (
        compressed(body[:9], "gzip") + b"\0" + compressed(body[9:], "gzip") + b"\0"
        if members
        else compressed(body, "gzip")
    )
    client = build(
        key_file,
        Handler(
            httpx.Response(200, stream=httpx.ByteStream(wire), headers={"content-encoding": "gzip"})
        ),
    )
    response = await client.get(PATH, kind="daily")
    assert response.json == json.loads(body)
    exchange = response.exchange
    assert (exchange.body, exchange.complete, exchange.error) == (body, True, None)
    assert exchange.size == len(body)


@pytest.mark.parametrize("wbits", [zlib.MAX_WBITS, -zlib.MAX_WBITS])
async def test_deflate_in_both_of_its_wire_forms_still_decodes(key_file, wbits):
    packer = zlib.compressobj(wbits=wbits)
    body = b'{"steps": 7}'
    wire = packer.compress(body) + packer.flush()
    client = build(
        key_file,
        Handler(httpx.Response(200, content=wire, headers={"content-encoding": "deflate"})),
    )
    assert (await client.get(PATH, kind="daily")).json == {"steps": 7}


async def test_a_gzip_bomb_is_refused_without_inflating_it_into_memory(key_file, monkeypatch):
    monkeypatch.setattr(garmin, "MAX_DECODED_BYTES", 1024 * 1024)
    bomb = gzip.compress(b"\0" * (64 * 1024 * 1024), compresslevel=9)
    client = build(
        key_file, Handler(httpx.Response(200, content=bomb, headers={"content-encoding": "gzip"}))
    )
    exchanges: list = []
    tracemalloc.start()
    try:
        with pytest.raises(GarminResponseRefused):
            await client.get(PATH, kind="daily", exchanges=exchanges)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 16 * 1024 * 1024, f"peak allocation {peak} is not bounded"
    refused = exchanges[-1]
    assert (refused.complete, refused.body) == (False, b"")
    assert refused.error == "response_too_large"
    assert refused.row()["complete"] is False


async def test_a_refused_compressed_body_is_never_kept_unscanned(key_file, monkeypatch):
    # A credential inside compressed bytes could not be masked, so those bytes are not kept.
    monkeypatch.setattr(garmin, "MAX_DECODED_BYTES", 64)
    handler = Handler(gzipped(b'{"echo": "' + TOKEN.encode() + b'"}' + b" " * 4096))
    exchanges: list = []
    with pytest.raises(GarminResponseRefused):
        await build(key_file, handler).get(PATH, kind="daily", exchanges=exchanges)
    assert exchanges[-1].body == b""


async def test_an_uncompressed_body_over_the_wire_budget_keeps_what_arrived(key_file, monkeypatch):
    monkeypatch.setattr(garmin, "MAX_WIRE_BYTES", 1024)
    body = b"x" * 4096
    exchanges: list = []
    with pytest.raises(GarminResponseRefused):
        await build(key_file, Handler(httpx.Response(200, content=body))).get(
            PATH, kind="daily", exchanges=exchanges
        )
    kept = exchanges[-1]
    assert kept.complete is False
    assert kept.body and body.startswith(kept.body) and len(kept.body) <= 4096
    assert kept.error == "response_too_large"


@pytest.mark.parametrize("encoding", [None, "identity"])
@pytest.mark.parametrize("delta", [-1, 0, 1])
async def test_uncompressed_decoded_budget_boundary(key_file, monkeypatch, encoding, delta):
    body = b'{"a": "' + b"y" * 100 + b'"}'
    monkeypatch.setattr(garmin, "MAX_WIRE_BYTES", len(body) * 2)
    monkeypatch.setattr(garmin, "MAX_DECODED_BYTES", len(body) - delta)
    headers = {"content-encoding": encoding} if encoding else {}
    response = httpx.Response(200, stream=httpx.ByteStream(body), headers=headers)
    handler, exchanges = Handler(response), []
    client = build(key_file, handler)
    try:
        if delta > 0:
            monkeypatch.setattr(
                garmin, "decode_response_body", lambda _: pytest.fail("JSON reached")
            )
            with pytest.raises(GarminResponseRefused):
                await client.get(PATH, kind="d", exchanges=exchanges)
        else:
            assert (await client.get(PATH, kind="d", exchanges=exchanges)).json == json.loads(body)
        kept = exchanges[-1]
        assert kept.body == body and kept.size == len(body)
        assert kept.sha256 == hashlib.sha256(body).hexdigest()
        assert kept.complete == (delta <= 0)
        assert kept.error == ("response_too_large" if delta > 0 else None)
        assert response.is_closed and handler.paths().count(PATH) == 1
    finally:
        await client.aclose()


async def test_a_rejected_response_is_not_retried(key_file, monkeypatch):
    monkeypatch.setattr(garmin, "MAX_WIRE_BYTES", 16)
    handler = Handler(httpx.Response(200, content=b"z" * 1024))
    with pytest.raises(GarminResponseRefused):
        await build(key_file, handler).get(PATH, kind="daily")
    assert len([r for r in handler.requests if r.url.path == PATH]) == 1


async def test_an_oversized_token_response_is_refused_and_not_used(key_file, monkeypatch):
    monkeypatch.setattr(garmin, "MAX_TOKEN_BYTES", 32)

    class HugeToken(FakeGarmin):
        def __call__(self, request):
            if request.url.path == garmin.TOKEN_PATH:
                self.token_requests += 1
                return httpx.Response(200, content=b"t" * 4096)
            return super().__call__(request)

    handler = HugeToken()
    exchanges: list = []
    with pytest.raises(GarminResponseRefused):
        await build(key_file, handler).get(PATH, kind="daily", exchanges=exchanges)
    assert exchanges[-1].kind == TOKEN_EXCHANGE
    assert exchanges[-1].complete is False
    assert b"t" * 32 not in exchanges[-1].body
    assert not any(r.url.path == PATH for r in handler.requests)


class Stalling(httpx.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        import asyncio

        yield b"{"
        await asyncio.sleep(30)


async def test_a_body_that_never_finishes_ends_as_a_retryable_failure(key_file, monkeypatch):
    monkeypatch.setattr(bounded_http, "MAX_READ_SECONDS", 0.05)
    handler = Handler(lambda _: httpx.Response(200, stream=Stalling()))
    exchanges: list = []
    with pytest.raises(GarminUnavailableError):
        await build(key_file, handler).get(PATH, kind="daily", exchanges=exchanges)
    assert len([r for r in handler.requests if r.url.path == PATH]) == garmin.MAX_ATTEMPTS


async def test_an_encoding_the_client_did_not_ask_for_is_refused(key_file):
    handler = Handler(httpx.Response(200, content=b"\xff\xff", headers={"content-encoding": "br"}))
    exchanges: list = []
    with pytest.raises(GarminResponseRefused):
        await build(key_file, handler).get(PATH, kind="daily", exchanges=exchanges)
    assert exchanges[-1].error == "unsupported_encoding"
    sent = [r for r in handler.requests if r.url.path == PATH][0]
    assert sent.headers["accept-encoding"] == "gzip, deflate"


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "raw-deflate"])
async def test_first_valid_json_stream_does_not_hide_later_stream(key_file, encoding):
    wire = compressed(b"{}", encoding) + compressed(b"{}", encoding)
    handler = Handler(
        lambda _: httpx.Response(
            200,
            stream=httpx.ByteStream(wire),
            headers={"content-encoding": encoding.removeprefix("raw-")},
        )
    )
    exchanges = []
    error = GarminResponseDecodeError if encoding == "gzip" else GarminUnavailableError
    with pytest.raises(error):
        await build(key_file, handler).get(PATH, kind="daily", exchanges=exchanges)
    if encoding == "gzip":
        assert exchanges[-1].body == b"{}{}" and exchanges[-1].complete
    else:
        assert len([r for r in handler.requests if r.url.path == PATH]) == garmin.MAX_ATTEMPTS
        assert all(e.kind == TOKEN_EXCHANGE for e in exchanges)


@pytest.mark.parametrize(
    "oversized, encoding", [(False, "gzip"), (True, "gzip"), (True, None), (True, "identity")]
)
async def test_body_failure_preserves_backfill_and_archive(
    sessions, key_file, monkeypatch, oversized, encoding, caplog
):
    monkeypatch.setattr(garmin, "MAX_DECODED_BYTES", 256)
    monkeypatch.setattr(garmin, "MAX_WIRE_BYTES", 8192)
    fake, clock = FakeGarmin(), Clock()
    pull = service(sessions, fake, key_file, clock)

    def pack(body, damage="valid"):
        return compressed(body, "gzip", damage) if encoding == "gzip" else body

    wire = pack(b"[]")

    def handler(request):
        if request.method == "POST":
            return fake(request)
        fake.requests.append(request)
        return httpx.Response(
            200,
            stream=httpx.ByteStream(
                wire if request.url.path.endswith(f"/{TODAY}") else pack(b"[]")
            ),
            headers={"content-encoding": encoding} if encoding else {},
        )

    await pull.client.http.aclose()
    pull.client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        assert not (await pull.run(["steps"])).failed
        success = (await pull.status())["kinds"]["steps"]["last_success_at"]
        secret = f" {TOKEN} {KEY} ".encode()
        wire += pack(secret * 10 if oversized else secret, "valid" if oversized else "checksum")
        fake.requests.clear()
        since = TODAY - timedelta(days=28)
        report = await pull.backfill(since, TODAY, kinds=["steps"])
        assert report["failed"] and not report["kinds"]["steps"]["complete"]
        state = (await pull.status())["kinds"]["steps"]
        assert state["last_success_at"] == success
        assert state["backfill"]["next"] == str(TODAY)
        assert len(fake.requests) == 1 + (1 if oversized else garmin.MAX_ATTEMPTS)
        async with sessions() as session:
            rows = (await session.scalars(select(GarminApiResponse))).all()
            assert not (await session.scalars(select(GarminRawRecord))).all()
            if oversized:
                assert rows[-1].error == "response_too_large"
                expected = b"" if encoding == "gzip" else wire
                assert not rows[-1].complete
                assert rows[-1].body == (garmin.REDACTED_BODY if expected else b"")
                assert rows[-1].body_size == len(expected)
                assert rows[-1].body_sha256 == hashlib.sha256(expected).hexdigest()
            else:
                assert len(rows) == 3  # No scanned body exists for corrupt compression.
            assert TOKEN not in str([r.__dict__ for r in rows])
            assert KEY not in str([r.__dict__ for r in rows])
        assert TOKEN not in caplog.text and KEY not in caplog.text
        wire = pack(b"[]")
        resumed = await pull.backfill(since, TODAY, kinds=["steps"])
        assert not resumed["failed"] and resumed["kinds"]["steps"]["complete"]
        assert resumed["kinds"]["steps"]["resumed_from"] == str(TODAY)
    finally:
        await pull.aclose()


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("recover", [False, True])
async def test_close_timeout_uses_existing_retry_budget(
    key_file, monkeypatch, caplog, method, recover
):
    monkeypatch.setattr(garmin, "MIN_INTERVAL", 0)
    fake = ClosingGarmin(method, recover=recover, expire=True, close_error=TimeoutError)
    client, exchanges, waits = build(key_file, fake), [], []

    async def sleep(delay):
        waits.append(delay)

    client.sleep = sleep
    try:
        if recover:
            assert (await client.get(PATH, kind="daily", exchanges=exchanges)).json == []
        else:
            with pytest.raises(GarminUnavailableError) as err:
                await client.get(PATH, kind="daily", exchanges=exchanges)
            assert err.value.__cause__ is err.value.__context__ is None
            safe_chain(err.value)
        attempts = 2 if recover else garmin.MAX_ATTEMPTS
        assert len([r for r in fake.requests if r.method == method]) == attempts
        assert waits == [garmin.backoff(n) for n in range(attempts - 1)]
        assert scan([e.row() for e in exchanges], TOKEN, KEY) == []
        assert TOKEN not in caplog.text and KEY not in caplog.text
        for stream in fake.streams:
            stream.assert_settled()
    finally:
        await client.aclose()


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("during_read", [False, True])
async def test_cancel_keeps_garmin_ownership_until_close_settles(
    sessions, key_file, method, during_read, caplog
):
    fake = ClosingGarmin(method, read="stall" if during_read else "eof", close_error=TimeoutError)
    fake.path = f"/usersummary-service/stats/steps/daily/{TODAY}/{TODAY}"
    pull = service(sessions, fake, key_file, Clock())
    held = asyncio.Event()

    @asynccontextmanager
    async def ownership(_):
        held.set()
        try:
            yield sessions
        finally:
            held.clear()

    pull.owned_by(ownership)
    task = asyncio.create_task(pull.backfill(TODAY, TODAY, kinds=["steps"]))
    try:
        await fake.started.wait()
        stream = fake.streams[0]
        await repeated_cancel(task, stream, during_read=during_read)
        assert held.is_set() and len(fake.streams) == 1
        stream.cleanup.set()
        with pytest.raises(asyncio.CancelledError) as err:
            await task
        safe_chain(err.value)
        assert not held.is_set()
        stream.assert_settled()
        assert len([r for r in fake.requests if r.method == method]) == 1
        assert (await pull.status())["kinds"]["steps"]["backfill"]["next"] == str(TODAY)
        assert not await archived(sessions)
        assert TOKEN not in caplog.text and KEY not in caplog.text
    finally:
        await dispose(task, *fake.streams)
        await pull.aclose()


async def test_close_timeout_preserves_backfill_checkpoint_and_existing_archive(
    sessions, key_file, caplog
):
    fake = ClosingGarmin(expire=True, close_error=TimeoutError)
    fake.path = f"/usersummary-service/stats/steps/daily/{TODAY}/{TODAY}"
    fake.bodies[fake.path] = [daily_steps(TODAY)]
    fake.fail = False
    pull = service(sessions, fake, key_file, Clock())
    try:
        assert not (await pull.run(["steps"])).failed
        # Keep a real record from an earlier successful window as well as HTTP evidence.
        assert not (await pull.backfill(TODAY, TODAY, kinds=["steps"]))["failed"]
        before = await archived(sessions)
        success = (await pull.status())["kinds"]["steps"]["last_success_at"]
        fake.fail = True
        since = TODAY - timedelta(days=28)
        report = await pull.backfill(since, TODAY, kinds=["steps"])
        state = (await pull.status())["kinds"]["steps"]
        assert report["failed"] and not report["kinds"]["steps"]["complete"]
        assert state["last_success_at"] == success and state["backfill"]["next"] == str(TODAY)
        rows = await archived(sessions)
        assert rows[: len(before)] == before and len(rows) == len(before) + 1
        assert len(fake.streams) == garmin.MAX_ATTEMPTS
        for stream in fake.streams:
            stream.assert_settled()
        async with sessions() as session:
            records = (await session.scalars(select(GarminRawRecord))).all()
            assert len(records) == 1 and records[0].body == daily_steps(TODAY)
        assert scan(rows, TOKEN, KEY) == []
        assert TOKEN not in caplog.text and KEY not in caplog.text
        fake.fail = False
        resumed = await pull.backfill(since, TODAY, kinds=["steps"])
        assert not resumed["failed"] and resumed["kinds"]["steps"]["complete"]
        assert resumed["kinds"]["steps"]["resumed_from"] == str(TODAY)
        assert (await archived(sessions))[: len(rows)] == rows
    finally:
        await pull.aclose()
