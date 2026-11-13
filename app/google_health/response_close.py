"""One bounded close per response, shared by normal and cancelled requests."""

from __future__ import annotations

import httpx

from app.core.response_close import close_response as bounded_close

# Independent of HTTPX's socket timeouts: a stream's aclose() can wait without I/O.
# Tests shorten this budget without changing the retry count or request policy.
RESPONSE_CLOSE_SECONDS = 10.0


class ResponseCloseTimeout(httpx.TimeoutException):
    """A transient transport failure with no response or credential text."""

    def __init__(self) -> None:
        super().__init__("Google Health response close exceeded its time budget")


async def close_response(response: httpx.Response) -> None:
    """Close in the owning task, so timeout cancels and settles the same operation.

    There is no detached inner aclose task. The caller shields this task from stop
    signals and keeps it in the request's cleanup list. Timeout is a transport error,
    using the existing retry limit; cancellation by request cleanup stays cancellation.
    """
    await bounded_close(
        response, seconds=RESPONSE_CLOSE_SECONDS, timeout_error=ResponseCloseTimeout
    )
