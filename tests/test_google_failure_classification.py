"""Request failure classification retains retry, capture and privacy contracts."""

from unittest.mock import AsyncMock

import httpx
import pytest
from support.google_client import Tokens

from app.core.config import Settings
from app.google_health.client import GoogleHealthClient, GoogleHealthError, ResponseStreamError
from app.google_health.errors import GoogleHealthErrorCode
from app.sync.errors import error_summary, is_transient


class BrokenBody(httpx.AsyncByteStream):
    def __init__(self, failure):
        self.failure = failure
        self.closed = False

    async def __aiter__(self):
        yield b'{"healthUserId":'
        raise self.failure

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize(
    "failure, expected, transient, attempts",
    [
        (httpx.ReadError("synthetic-private"), GoogleHealthError, True, 2),
        (httpx.DecodingError("synthetic-private"), GoogleHealthError, True, 2),
        (ValueError("synthetic-private"), ResponseStreamError, False, 1),
    ],
)
async def test_partial_stream_failure_preserves_bytes_and_retry_class(
    failure, expected, transient, attempts
):
    streams = []

    def respond(request):
        stream = BrokenBody(failure)
        streams.append(stream)
        return httpx.Response(200, stream=stream)

    archive, sleep = AsyncMock(), AsyncMock()
    settings = Settings(
        _env_file=None, google_api_min_request_interval_seconds=0, google_api_max_retries=1
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = GoogleHealthClient(settings, Tokens(), http, sleep=sleep, archive=archive)
        with pytest.raises(expected) as caught:
            await client.get_identity()
    assert type(caught.value) is expected
    assert caught.value.code is (GoogleHealthErrorCode.CONNECTION if transient else None)
    assert is_transient(caught.value) is transient
    assert "synthetic-private" not in str(caught.value) + error_summary(caught.value)
    assert len(streams) == attempts and all(stream.closed for stream in streams)
    assert sleep.await_count == attempts - 1
    archive.keep.assert_awaited_once()
    captured = archive.keep.await_args.args[0]
    assert len(captured) == attempts
    assert all(
        response.body == b'{"healthUserId":' and not response.complete for response in captured
    )
