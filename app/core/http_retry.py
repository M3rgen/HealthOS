"""HTTP retry loop; authentication and response capture remain with each client."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from math import isfinite
from typing import Any


@dataclass(frozen=True)
class RetryFailure:
    status: int | None = None
    header: str | None = None
    cause: BaseException | None = None


@dataclass(frozen=True)
class Repeat:
    """Authentication retry, independent of the transient failure budget."""

    reset: bool = False


@dataclass(frozen=True)
class Success[T]:
    value: T


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int
    max_delay: float
    backoff: Callable[[int], float]
    retry_after: Callable[[str | None], float | None]
    notify_exhausted: bool = False


def transient_status(status: int) -> bool:
    return status == 429 or 500 <= status < 600


def retry_after(
    header: str | None,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    parse_date: Callable[[str], Any] = parsedate_to_datetime,
    strict: bool = True,
) -> float | None:
    """Seconds or an HTTP date; unsafe upstream values fall back to backoff.

    ``strict=False`` keeps Garmin's parsing mode, but also requires finite seconds
    and an aware date. Recovery stays at parsing/conversion boundaries.
    """
    if header is None or (strict and not header):
        return None
    if not strict:
        try:
            delay = float(header)
        except ValueError:
            try:
                moment = parse_date(header)
            except (TypeError, ValueError, OverflowError, OSError):
                return None
            # RFC 2822's -0000 means an unknown timezone, not UTC.
            if moment.tzinfo is None:
                return None
            current = now()  # Clock failures are not malformed response headers.
            try:
                delay = (moment - current).total_seconds()
            except (TypeError, ValueError, OverflowError, OSError):
                return None
        except OverflowError:
            return None
        return max(0.0, delay) if isfinite(delay) else None
    try:
        delay = float(header)
    except ValueError:
        try:
            delay = (parse_date(header) - now()).total_seconds()
        except (TypeError, ValueError, OverflowError, OSError):
            return None
    except OverflowError:
        return None
    return max(0.0, delay) if isfinite(delay) else None


def backoff(
    attempt: int,
    *,
    base: float = 2,
    jitter: float = 0,
    ceiling: float = float("inf"),
) -> float:
    return float(min(ceiling, base**attempt + jitter))


async def request_with_retry[T](
    send: Callable[[int], Awaitable[Success[T] | RetryFailure | Repeat]],
    *,
    policy: RetryPolicy,
    sleep: Callable[[float], Awaitable[Any]],
    error: Callable[[RetryFailure, bool], Exception],
    notify: Callable[[RetryFailure, int, float], None],
) -> T:
    """Retry only classified failures, retaining each provider's budget and event order.

    ``error`` receives whether the upstream minimum exceeds the delay budget. A valid
    excessive Retry-After is never clamped, which would retry before the upstream permits.
    """
    attempt = 0
    while True:
        result = await send(attempt)
        if isinstance(result, Success):
            return result.value
        if isinstance(result, Repeat):
            if result.reset:
                attempt = 0
            continue
        exhausted = attempt >= policy.max_retries
        if exhausted and not policy.notify_exhausted:
            raise error(result, False) from result.cause
        delay = policy.retry_after(result.header) if result.status is not None else None
        if delay is None:
            delay = policy.backoff(attempt)
        deferred = delay > policy.max_delay
        if policy.notify_exhausted or (not exhausted and not deferred):
            notify(result, attempt, delay)
        if exhausted or deferred:
            raise error(result, deferred) from result.cause
        await sleep(delay)
        attempt += 1
