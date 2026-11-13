"""Complete compression layers are validated within shared output budgets."""

import asyncio
import zlib

import httpx
import pytest
from compressed_records import compressed
from http_close_records import ClosingStream, dispose, repeated_cancel, safe_chain

from app.core import bounded_http as reader
from app.core import response_close


class Stream(httpx.AsyncByteStream):
    def __init__(self, wire):
        self.wire, self.closed, self.reads = wire, False, 0

    async def __aiter__(self):
        for chunk in self.wire if isinstance(self.wire, list) else [self.wire]:
            self.reads += 1
            yield chunk

    async def aclose(self):
        self.closed = True


async def read(wire, encoding, limit=1024):
    stream = Stream(wire)
    response = httpx.Response(200, stream=stream, headers={"content-encoding": encoding})
    try:
        return await reader.read_bounded(
            response, max_wire_bytes=len(wire), max_decoded_bytes=limit
        )
    finally:
        assert stream.closed


@pytest.mark.parametrize("encoding", [None, "identity"])
@pytest.mark.parametrize("wire_limit, decoded_limit", [(100, 31), (31, 100), (31, 31)])
@pytest.mark.parametrize("length", [30, 31, 32])
@pytest.mark.parametrize("claimed", [None, "1", "10000"])
async def test_uncompressed_budgets_stop_at_the_crossing_chunk(
    encoding, wire_limit, decoded_limit, length, claimed
):
    body = b"x" * length
    stream = Stream([body[:15], body[15:], b"unread" if length == 32 else b""])
    headers = {"content-encoding": encoding} if encoding else {}
    if claimed is not None:
        headers["content-length"] = claimed
    response = httpx.Response(200, stream=stream, headers=headers)
    result = await reader.read_bounded(
        response, max_wire_bytes=wire_limit, max_decoded_bytes=decoded_limit
    )
    assert result == reader.ReadBody(body, reader.TOO_LARGE if length == 32 else None)
    assert stream.reads == (2 if length == 32 else 3)
    assert stream.closed and response.is_closed


@pytest.mark.parametrize("chunk_size", [7, 64 * 1024])
@pytest.mark.parametrize("length", [30, 31, 32])
async def test_gzip_members_share_boundary_budget(monkeypatch, chunk_size, length):
    monkeypatch.setattr(reader, "_CHUNK_BYTES", chunk_size)
    payload = bytes(range(length))
    wire = compressed(payload[:15], "gzip") + b"\0\0" + compressed(payload[15:], "gzip")
    result = await read(wire + b"\0\0", "gzip", 31)
    assert result.body == (payload if length <= 31 else b"")
    assert result.refused == (None if length <= 31 else reader.TOO_LARGE)


@pytest.mark.parametrize("damage", ["valid", "truncated", "checksum", "junk"])
@pytest.mark.parametrize("chunk_size", [1, 7, 64 * 1024])
async def test_exact_limit_validates_later_empty_members(monkeypatch, damage, chunk_size):
    monkeypatch.setattr(reader, "_CHUNK_BYTES", chunk_size)
    tail = b"junk" if damage == "junk" else compressed(b"", "gzip", damage)
    wire = compressed(b"x" * 31, "gzip") + b"\0" + tail + b"\0"
    if damage == "valid":
        assert (await read(wire, "gzip", 31)).body == b"x" * 31
    else:
        with pytest.raises(httpx.DecodingError):
            await read(wire, "gzip", 31)


@pytest.mark.parametrize("wire", [b"", b"\0", b"\0" + compressed(b"{}", "gzip")])
async def test_gzip_requires_a_first_member(wire):
    with pytest.raises(httpx.DecodingError):
        await read(wire, "gzip")


@pytest.mark.parametrize("encoding", ["deflate", "raw-deflate"])
@pytest.mark.parametrize("damage", ["junk", "zero", "stream"])
@pytest.mark.parametrize("chunk_size", [1, 7, 64 * 1024])
async def test_deflate_suffix_is_final_without_raw_reinterpretation(
    monkeypatch, encoding, damage, chunk_size
):
    factory, attempts = zlib.decompressobj, []

    def inflater(wbits):
        attempts.append(wbits)
        return factory(wbits)

    monkeypatch.setattr(reader, "_CHUNK_BYTES", chunk_size)
    monkeypatch.setattr(zlib, "decompressobj", inflater)
    wire = compressed(b"{}", encoding, damage)
    with pytest.raises(httpx.DecodingError):
        await read(wire, "deflate")
    assert attempts == ([15] if encoding == "deflate" else [15, -15])


@pytest.mark.parametrize("inner", ["gzip", "deflate", "raw-deflate"])
@pytest.mark.parametrize("outer", ["gzip", "deflate", "raw-deflate"])
@pytest.mark.parametrize("stage", ["inner", "outer", "valid", "budget", "intermediate"])
async def test_every_stacked_layer_is_bounded_and_complete(inner, outer, stage):
    payload = bytes(range(200)) if stage == "intermediate" else b"x" * 200
    middle = compressed(payload, inner, "junk" if stage == "inner" else "valid")
    wire = compressed(middle, outer, "junk" if stage == "outer" else "valid")
    encoding = inner.removeprefix("raw-") + ", identity, " + outer.removeprefix("raw-")
    if stage in {"inner", "outer"}:
        with pytest.raises(httpx.DecodingError):
            await read(wire, encoding)
    else:
        result = await read(wire, encoding, len(middle) if stage == "budget" else 200)
        refused = stage in {"budget", "intermediate"}
        assert result.body == (b"" if refused else payload)
        assert result.refused == (reader.TOO_LARGE if refused else None)


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "raw-deflate", "gzip-members"])
async def test_bomb_produces_only_one_excess_byte(monkeypatch, encoding):
    factory, produced = zlib.decompressobj, []
    wire = compressed(b"x" * (2 * 1024 * 1024), encoding.removesuffix("-members"))
    if encoding == "gzip-members":
        wire = compressed(b"{}", "gzip") + wire

    class Inflater:
        def __init__(self, wbits):
            self.inner = factory(wbits)

        def decompress(self, data, max_length):
            assert 0 < max_length <= 32 and len(data) <= 64 * 1024
            piece = self.inner.decompress(data, max_length)
            produced.append(len(piece))
            return piece

        def flush(self, *args):
            pytest.fail("flush cannot bound inflated output")

        def __getattr__(self, name):
            return getattr(self.inner, name)

    monkeypatch.setattr(zlib, "decompressobj", Inflater)
    result = await read(wire, encoding.removesuffix("-members").removeprefix("raw-"), 31)
    assert result == reader.ReadBody(b"", reader.TOO_LARGE)
    assert sum(produced) == 32


@pytest.mark.parametrize("phase", ["eof", "size", "read-error", "timeout"])
async def test_stalled_close_has_its_own_budget_and_settles(phase):
    stream = ClosingStream(
        expire=True,
        read_error=httpx.ReadError if phase == "read-error" else None,
        read="timeout" if phase == "timeout" else "eof",
    )
    response = httpx.Response(200, stream=stream)
    task = asyncio.create_task(
        reader.read_bounded(
            response, max_wire_bytes=1 if phase == "size" else 10, max_decoded_bytes=10
        )
    )
    try:
        error = (
            httpx.ReadError
            if phase == "read-error"
            else httpx.ReadTimeout
            if phase == "timeout"
            else response_close.ResponseCloseTimeout
        )
        with pytest.raises(error):
            await task
        stream.assert_settled()
    finally:
        await dispose(task, stream)


@pytest.mark.parametrize("during_read", [False, True])
@pytest.mark.parametrize("close_error", [None, httpx.CloseError, TimeoutError])
async def test_repeated_cancel_joins_close_without_replacing_cancellation(during_read, close_error):
    stream = ClosingStream(read="stall" if during_read else "eof", close_error=close_error)
    task = asyncio.create_task(
        reader.read_bounded(
            httpx.Response(200, stream=stream), max_wire_bytes=10, max_decoded_bytes=10
        )
    )
    try:
        await repeated_cancel(task, stream, during_read=during_read)
        stream.cleanup.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        safe_chain(caught.value)
        stream.assert_settled()
    finally:
        await dispose(task, stream)


async def test_close_timeout_drops_upstream_timeout_context():
    stream = ClosingStream(expire=True, close_error=TimeoutError)
    with pytest.raises(response_close.ResponseCloseTimeout) as caught:
        await reader.read_bounded(
            httpx.Response(200, stream=stream), max_wire_bytes=10, max_decoded_bytes=10
        )
    assert caught.value.__cause__ is caught.value.__context__ is None
    safe_chain(caught.value)
    stream.assert_settled()
