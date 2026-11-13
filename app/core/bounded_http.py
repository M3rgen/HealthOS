"""Read an HTTP response body under byte and time budgets, before it can exhaust memory.

``httpx`` inflates a compressed body with no limit the moment it is read: a 521 KB gzip becomes
512 MiB in one call. So the body is received as a stream of wire bytes, counted, and inflated
with the output bounded. The caller keeps the transport-level failures (raised as
``httpx.HTTPError``) and decides what a refused body means for its retries and its archive.
"""

from __future__ import annotations

import asyncio
import zlib
from dataclasses import dataclass

import httpx

from app.core.response_close import finish_response

# Only what the budgeted reader can inflate is asked for.
ACCEPT_ENCODING = "gzip, deflate"
# A body that is still arriving after this long is a stalled peer, whatever its pace: the read
# timeout only limits the gap between two chunks.
MAX_READ_SECONDS = 120.0
_CHUNK_BYTES = 64 * 1024
TOO_LARGE = "response_too_large"
UNSUPPORTED_ENCODING = "unsupported_encoding"


class _DecodedTooLarge(ValueError):
    pass


class _Truncated(ValueError):
    pass


class _TrailingEncodedBytes(ValueError):
    pass


@dataclass(frozen=True)
class ReadBody:
    """``refused`` is None for a complete body, or why the body was not accepted.

    A refused compressed body is not kept at all: its bytes could not be searched for
    credentials, and the refusal itself is the evidence.
    """

    body: bytes
    refused: str | None = None


def _encodings(response: httpx.Response) -> list[str]:
    values = response.headers.get_list("content-encoding", split_commas=True)
    return [name for name in (value.strip().lower() for value in values) if name != "identity"]


def _inflate(wire: memoryview, wbits: int, output: bytearray, limit: int) -> int:
    """Append one bounded stream and return its consumed input, checking checksum and EOF.

    All gzip members append to the same output budget. Never flush(): its length
    argument does not cap allocation. Bounded input slices also keep zlib's copies
    of unused/unconsumed input small; one excess output byte detects overflow.
    """
    inflater = zlib.decompressobj(wbits)
    offset = 0
    while True:
        chunk = wire[offset : offset + _CHUNK_BYTES]
        remaining = limit - len(output)
        piece = inflater.decompress(chunk, min(_CHUNK_BYTES, remaining + 1))
        if len(piece) > remaining:
            raise _DecodedTooLarge()
        output.extend(piece)
        offset += len(chunk)
        if inflater.eof:
            return offset - len(inflater.unused_data)
        offset -= len(inflater.unconsumed_tail)
        if not chunk and not piece:
            raise _Truncated()


def _gzip(wire: bytes, limit: int) -> bytes:
    """Validate every member, allowing zero padding only between/after members."""
    if not wire:
        raise _Truncated()
    output = bytearray()
    view = memoryview(wire)
    offset = 0
    while offset < len(view):
        offset += _inflate(view[offset:], zlib.MAX_WBITS | 16, output, limit)
        while offset < len(view) and view[offset] == 0:
            offset += 1
    return bytes(output)


def _inflate_single(wire: memoryview, wbits: int, output: bytearray, limit: int) -> None:
    """Deflate permits neither padding nor concatenation after its single stream.

    A complete wrapped stream with a suffix must not trigger raw fallback: this
    rejection deliberately differs from zlib.error, as do truncation and overflow.
    """
    if _inflate(wire, wbits, output, limit) != len(wire):
        raise _TrailingEncodedBytes()


def _deflate(wire: bytes, limit: int) -> bytes:
    """Accept wrapped/legacy raw deflate, requiring EOF and full input consumption."""
    output = bytearray()
    view = memoryview(wire)
    try:
        _inflate_single(view, zlib.MAX_WBITS, output, limit)
    except zlib.error:  # "deflate" is also sent without its zlib header
        output.clear()
        _inflate_single(view, -zlib.MAX_WBITS, output, limit)
    return bytes(output)


def _decode(wire: bytes, encodings: list[str], limit: int) -> bytes:
    body = wire
    for encoding in reversed(encodings):
        if encoding == "gzip":
            body = _gzip(body, limit)
        else:
            body = _deflate(body, limit)
    return body


async def _receive(
    response: httpx.Response, max_wire_bytes: int, max_decoded_bytes: int
) -> tuple[bytearray, bool]:
    """The delivered bytes, and whether either applicable budget was exceeded.

    With no compression, every wire byte is also a decoded byte: stop at the
    smaller budget while receiving, without trusting Content-Length. Compressed
    layers instead enforce their output budgets during decoding.
    The crossing chunk is kept whole as evidence; no later chunk is requested.
    """
    limit = max_wire_bytes
    if not _encodings(response):
        limit = min(limit, max_decoded_bytes)
    wire = bytearray()
    try:
        async with asyncio.timeout(MAX_READ_SECONDS):
            # aiter_raw() refuses a body already read; ``stream`` is the wire in every case.
            if not isinstance(response.stream, httpx.AsyncByteStream):
                raise RuntimeError("Expected an asynchronous response stream")
            async for chunk in response.stream:
                wire.extend(chunk)
                if len(wire) > limit:
                    return wire, True
    except TimeoutError:
        raise httpx.ReadTimeout("response body exceeded its time budget") from None
    return wire, False


async def read_bounded(
    response: httpx.Response, *, max_wire_bytes: int, max_decoded_bytes: int
) -> ReadBody:
    """The decoded body of a response opened with ``stream=True``; always closes it.

    Corrupt compression raises ``httpx.DecodingError``, as reading it through ``httpx`` did.
    """
    failure = None
    wire, exceeded = bytearray(), False
    try:
        wire, exceeded = await _receive(response, max_wire_bytes, max_decoded_bytes)
    except BaseException as exc:
        failure = exc
    failure = await finish_response(response, failure)
    if failure is not None:
        raise failure
    encodings = _encodings(response)
    if exceeded:
        return ReadBody(b"" if encodings else bytes(wire), TOO_LARGE)
    if any(encoding not in ("gzip", "deflate") for encoding in encodings):
        return ReadBody(b"", UNSUPPORTED_ENCODING)
    try:
        return ReadBody(_decode(bytes(wire), encodings, max_decoded_bytes))
    except _DecodedTooLarge:
        return ReadBody(b"", TOO_LARGE)
    except (zlib.error, _Truncated, _TrailingEncodedBytes) as exc:
        raise httpx.DecodingError("response content decoding failed") from exc
