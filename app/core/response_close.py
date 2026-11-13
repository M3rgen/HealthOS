"""Bound response cleanup without releasing its owner while a close is pending."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence

import httpx

# Socket timeouts do not bound a stream's aclose(), which may wait without I/O.
RESPONSE_CLOSE_SECONDS = 10.0


class ResponseCloseTimeout(httpx.TimeoutException):
    """A retryable transport failure with no upstream message or request attached."""

    def __init__(self) -> None:
        super().__init__("HTTP response close exceeded its time budget")


async def close_response(
    response: httpx.Response,
    *,
    seconds: float | None = None,
    timeout_error: Callable[[], httpx.TimeoutException] = ResponseCloseTimeout,
) -> None:
    """Cancel and settle the same aclose operation when its independent budget expires.

    Run in an owned task: HTTPX marks a response closed before awaiting the stream,
    so restarting aclose after cancellation cannot finish the original operation.
    Raise outside the timeout handler to discard even implicit exception context:
    a stream may replace timeout cancellation with a credential-bearing TimeoutError.
    """
    try:
        async with asyncio.timeout(RESPONSE_CLOSE_SECONDS if seconds is None else seconds):
            await response.aclose()
    except TimeoutError:
        pass
    else:
        return
    raise timeout_error()


async def cancel_closes(closing: Sequence[asyncio.Future]) -> None:
    """Cancel once and join cleanup despite repeated stop signals.

    Retrieve finished failures too. Neither a detached task nor an unobserved
    exception may outlive request ownership or the keyset refresh lock.
    """
    if not closing:
        return
    for close in closing:
        if not close.done():
            close.cancel()
    settled = asyncio.gather(*closing, return_exceptions=True)
    while not settled.done():
        try:
            await asyncio.shield(settled)
        except asyncio.CancelledError:
            continue
    settled.result()


async def finish_response(
    response: httpx.Response, failure: BaseException | None = None
) -> BaseException | None:
    """Close exactly once, preserving the first failure unless cancellation intervenes.

    The caller leaves its read/status exception handler before entering here and
    raises the returned failure afterwards. Cleanup therefore cannot add a read
    error (possibly containing credentials) to a safe close timeout's chain.
    An earlier cancellation still gets bounded cleanup, and always stays cancellation.
    """
    close = asyncio.create_task(close_response(response))
    try:
        await asyncio.shield(close)
    except asyncio.CancelledError as exc:
        await cancel_closes([close])
        return exc
    except Exception as exc:
        if failure is None:
            return exc
    return failure
