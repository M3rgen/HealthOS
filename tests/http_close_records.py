"""Event-controlled HTTP cleanup, with invented credential echoes on failure."""

import asyncio

import httpx
from support.loop_clock import advance_loop_clock

from app.core import bounded_http, response_close

SECRET = "synthetic-close-credential"


class ClosingStream(httpx.AsyncByteStream):
    def __init__(self, wire=b"{}", *, expire=False, read_error=None, close_error=None, read="eof"):
        self.wire, self.expire, self.read = wire, expire, read
        self.read_error, self.close_error = read_error, close_error
        self.reading, self.closing, self.cancelling = (asyncio.Event() for _ in range(3))
        self.release, self.cleanup, self.closed = (asyncio.Event() for _ in range(3))
        self.cleanup.set()
        self.close_calls, self.owner = 0, None

    async def __aiter__(self):
        yield self.wire
        self.reading.set()
        if self.read_error:
            raise self.read_error(SECRET)
        if self.read != "eof":
            if self.read == "timeout":
                advance_loop_clock(bounded_http.MAX_READ_SECONDS + 1)
            await asyncio.Event().wait()

    async def aclose(self):
        self.close_calls += 1
        self.owner = asyncio.current_task()
        self.closing.set()
        try:
            if self.expire:
                advance_loop_clock(response_close.RESPONSE_CLOSE_SECONDS + 1)
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelling.set()
                await self.cleanup.wait()
                if self.close_error:
                    raise self.close_error(SECRET) from None
                raise
            if self.close_error:
                raise self.close_error(SECRET)
        finally:
            self.closed.set()

    def assert_settled(self):
        assert self.close_calls == 1 and self.closed.is_set() and self.owner.done()


async def repeated_cancel(task, stream, *, during_read=False):
    stream.cleanup.clear()
    if during_read:
        await stream.reading.wait()
        task.cancel()
    await stream.closing.wait()
    task.cancel()
    await stream.cancelling.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and not stream.closed.is_set()
    # The caller checks its lock/ownership here, while cleanup still holds the task.


async def dispose(task, *streams):
    for stream in streams:
        stream.release.set()
        stream.cleanup.set()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def safe_chain(error):
    """Walk hidden contexts too; `from None` alone is insufficient for credentials."""
    seen, pending = set(), [error]
    while pending:
        exc = pending.pop()
        if exc is None or id(exc) in seen:
            continue
        seen.add(id(exc))
        assert SECRET not in str(exc)
        pending.extend([exc.__cause__, exc.__context__])
