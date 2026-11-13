from __future__ import annotations

import asyncio
import logging
import random
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from app.core import http_retry
from app.core.config import Settings
from app.core.logging import error_diagnostics, event
from app.core.provenance import current_context_id, current_source, provenance
from app.core.response_close import cancel_closes
from app.google_health import strict_json
from app.google_health.capture import CapturedResponse, ResponseArchive, kept_headers, safe_query
from app.google_health.data_types import AccessMethod, DataTypeSpec, query_iso
from app.google_health.errors import GoogleHealthErrorCode
from app.google_health.errors import InvalidPageTokenError as InvalidPageTokenError
from app.google_health.errors import LocalRequestRejectedError as LocalRequestRejectedError
from app.google_health.errors import ResponseBodyTooLargeError as ResponseBodyTooLargeError
from app.google_health.errors import ResponseStreamError as ResponseStreamError
from app.google_health.errors import RetryDeferredError as RetryDeferredError
from app.google_health.exchange import ReceivedBody, RequestContext
from app.google_health.oauth import TokenProvider
from app.google_health.response_body import ResponseBodyLimitExceeded
from app.google_health.response_body import read_body as _read_body
from app.google_health.response_decode import ResponseDecodeRejected
from app.google_health.response_decode import decode as _decode
from app.google_health.response_handoff import RedirectResponseReceived, own_redirect
from app.google_health.response_page import (  # noqa: F401 - re-exported error types
    DuplicateResponseKeyError,
    ErrorResponsePayloadError,
    GoogleHealthError,
    InvalidCollectionItemError,
    InvalidCollectionResponseError,
    InvalidPaginationResponseError,
    InvalidResponseError,
    NonStandardResponseConstantError,
    UnstorablePaginationResponseError,
    page_values,
)

logger = logging.getLogger(__name__)

# One request's retry wait must stay within the existing backoff ceiling. A valid
# Retry-After above it ends the request; clamping would retry before Google permits.
MAX_RETRY_DELAY_SECONDS = 60.0


@dataclass(frozen=True)
class Page:
    data_points: list[dict]
    next_page_token: str | None
    # Every HTTP exchange behind this page (retries included), oldest first; the last one
    # is the response the data points were read from. Stored with the records
    # (app.services.archive.storage.store_page), in the same transaction.
    responses: tuple[CapturedResponse, ...] = ()
    # Set by iter_pages when next_page_token repeats a continuation of this attempt.
    # The records are stored; the token is never committed as a checkpoint.
    cyclic: bool = False


class GoogleHealthClient:
    def __init__(
        self,
        settings: Settings,
        tokens: TokenProvider,
        http: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        archive: ResponseArchive | None = None,
    ):
        self.settings = settings
        self.tokens = tokens
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(60, connect=20))
        self.sleep = sleep
        # Where responses go that no page transaction stores (failures, identity, devices,
        # probes); None (tests) drops them.
        self.archive = archive
        self._request_lock = asyncio.Lock()
        self._last_request_started = 0.0

    async def warm_up(self) -> None:
        """Obtain credentials once before starting a multi-type recovery."""
        await self.tokens.get()

    async def list_data_points(
        self,
        spec: DataTypeSpec,
        start: datetime,
        end: datetime,
        page_token: str | None = None,
        page_size: int | None = None,
    ) -> Page:
        requested_page_size = page_size or self.settings.google_api_page_size
        effective_page_size = (
            min(requested_page_size, 25)
            if spec.name in {"sleep", "exercise"}
            else requested_page_size
        )
        if spec.access_method == AccessMethod.rollup:
            start, end = minute_aligned_range(start, end)
            body = {
                "range": {"startTime": _iso(start), "endTime": _iso(end)},
                "windowSize": "60s",
                "pageSize": effective_page_size,
                "dataSourceFamily": "users/me/dataSourceFamilies/all-sources",
            }
            if page_token:
                body["pageToken"] = page_token
            payload, responses = await self._request(
                "POST",
                f"/users/me/dataTypes/{spec.name}/dataPoints:rollUp",
                data_type=spec.name,
                json=body,
            )
        else:
            params: dict[str, str | int] = {"pageSize": effective_page_size}
            if filter_expression := spec.filter_for(_iso(start), _iso(end, upper=True)):
                params["filter"] = filter_expression
            if spec.access_method == AccessMethod.reconcile:
                params["dataSourceFamily"] = "users/me/dataSourceFamilies/all-sources"
            if page_token:
                params["pageToken"] = page_token
            suffix = ":reconcile" if spec.access_method == AccessMethod.reconcile else ""
            payload, responses = await self._request(
                "GET",
                f"/users/me/dataTypes/{spec.name}/dataPoints{suffix}",
                data_type=spec.name,
                params=params,
            )
        try:
            points, token = page_values(payload, spec.access_method)
        except GoogleHealthError:
            # No Page will reach store_page: preserve this response and every retry
            # through the same cancellation-safe archive used for other rejections.
            await self.keep(responses)
            raise
        return Page(points, token, tuple(responses))

    async def get_identity(self, access_token: str | None = None) -> dict:
        with provenance(source="identity"):
            payload, responses = await self._request(
                "GET", "/users/me/identity", access_token=access_token
            )
        await self.keep(responses)
        return payload

    async def list_paired_devices(self) -> dict:
        payload, responses = await self._request("GET", "/users/me/pairedDevices")
        await self.keep(responses)
        return payload

    async def keep(self, responses: Sequence[CapturedResponse]) -> None:
        """Store responses outside a page transaction (failures, probes, identity).

        An archive error is logged, never raised. If the calling task is cancelled while
        the responses are being stored, the store still gets up to
        ``KEEP_ON_CANCEL_SECONDS`` to finish before the cancellation propagates.
        """
        if not responses or self.archive is None:
            return
        store = asyncio.ensure_future(self._store(responses))
        try:
            await asyncio.shield(store)
        except asyncio.CancelledError:
            await _finish(store, len(responses))
            raise

    async def keep_after(
        self,
        responses: Sequence[CapturedResponse],
        failure: BaseException,
        *,
        closing: Sequence[asyncio.Future] = (),
    ) -> None:
        """Keep responses whose page or request was cut short by ``failure``.

        Also on cancellation (a restart or deploy between fetching and storing): the bytes
        already received are stored (shielded, at most ``KEEP_ON_CANCEL_SECONDS``); the
        caller re-raises ``failure`` either way, so a cancellation is never swallowed.
        """
        if isinstance(failure, Exception):
            await self.keep(responses)
        else:
            # Close and archive concurrently within the same cancellation budget. A
            # stalled close must not consume the time available to preserve the bytes.
            cleanup = list(closing)
            if responses and self.archive is not None:
                cleanup.append(asyncio.ensure_future(self._store(responses)))
            if cleanup:
                try:
                    await _finish(asyncio.gather(*cleanup, return_exceptions=True), len(responses))
                finally:
                    # The archive may still finish in the background, as with keep().
                    # A close that used up its budget must not stay pending indefinitely.
                    await cancel_closes(closing)

    async def _store(self, responses: Sequence[CapturedResponse]) -> None:
        if self.archive is None:
            raise RuntimeError("Response archive is unavailable")
        try:
            await self.archive.keep(responses)
        except Exception as exc:
            # Database errors can include raw response bodies and request parameters.
            # Keep the exception, traceback and cause chain out of the LogRecord.
            error_type = type(exc).__name__
            diagnostics = error_diagnostics(exc)
        else:
            return
        # Leave the handler before logging: only the detached class name is needed.
        logger.error(
            "Google Health responses could not be archived",
            extra={
                "event": "RESPONSE_ARCHIVE_FAILED",
                "count": len(responses),
                "error_type": error_type,
                **diagnostics,
            },
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        access_token: str | None = None,
        data_type: str | None = None,
        **kwargs: Any,
    ) -> tuple[dict, list[CapturedResponse]]:
        """The decoded JSON object and every response received on the way to it.

        On failure the responses received so far are archived before the error propagates.
        """
        captured: list[CapturedResponse] = []
        closing: list[asyncio.Future] = []
        try:
            return await self._exchange(
                captured,
                closing,
                request_context=RequestContext(method, path, access_token, data_type, kwargs),
            )
        except BaseException as failure:
            await self.keep_after(captured, failure, closing=closing)
            raise

    async def _exchange(
        self,
        captured: list[CapturedResponse],
        closing: list[asyncio.Future],
        *,
        request_context: RequestContext,
    ) -> tuple[dict, list[CapturedResponse]]:
        await self._pace_request()
        refreshed = False

        async def send(attempt: int):
            nonlocal refreshed
            response, body, failure = await self._send_response(
                captured,
                closing,
                request_context=request_context,
            )
            if failure is not None or response is None:
                return http_retry.RetryFailure(cause=failure)
            if (
                response.status_code == 401
                and request_context.access_token is None
                and not refreshed
            ):
                self.tokens.invalidate()
                refreshed = True
                return http_retry.Repeat()
            if http_retry.transient_status(response.status_code):
                return http_retry.RetryFailure(
                    response.status_code, response.headers.get("Retry-After")
                )
            return http_retry.Success((_success_payload(response, body), captured))

        def error(failure: http_retry.RetryFailure, deferred: bool) -> Exception:
            if failure.status is None:
                return GoogleHealthError(
                    "Google Health API connection failed after retries",
                    code=GoogleHealthErrorCode.CONNECTION,
                )
            if deferred:
                return RetryDeferredError(
                    f"Google Health API retry deferred (HTTP {failure.status})",
                    http_status=failure.status,
                )
            return GoogleHealthError(
                f"Google Health API request failed after retries (HTTP {failure.status})",
                http_status=failure.status,
            )

        def notify(failure: http_retry.RetryFailure, attempt: int, delay: float) -> None:
            if failure.status == 429:
                event(logger, "RATE_LIMITED", retry_after_seconds=delay)
            event(logger, "RETRYING", attempt=attempt + 1, delay_seconds=delay)

        return await http_retry.request_with_retry(
            send,
            policy=http_retry.RetryPolicy(
                self.settings.google_api_max_retries,
                MAX_RETRY_DELAY_SECONDS,
                _backoff,
                _retry_after,
            ),
            sleep=self.sleep,
            error=error,
            notify=notify,
        )

    async def _send_response(
        self,
        captured: list[CapturedResponse],
        closing: list[asyncio.Future],
        *,
        request_context: RequestContext,
    ) -> tuple[httpx.Response | None, bytes, BaseException | None]:
        token = (
            request_context.access_token
            if request_context.access_token is not None
            else await self.tokens.get()
        )
        request = self.http.build_request(
            request_context.method,
            f"{self.settings.google_health_base_url}{request_context.path}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                # Uncompressed, so the stored body is the bytes on the wire.
                "Accept-Encoding": "identity",
            },
            **request_context.kwargs,
        )
        response: httpx.Response | None = None
        body = b""
        failure: BaseException | None = None
        request.extensions["healthos_capture_redirect"] = True
        if own_redirect not in self.http.event_hooks["response"]:
            self.http.event_hooks["response"].insert(0, own_redirect)
        try:
            # Override even an injected client's follow_redirects=True. Each
            # response must pass our status gate and reach the raw archive;
            # a redirect target must never supply a substitute success page.
            response = await self.http.send(request, stream=True, follow_redirects=False)
        except RedirectResponseReceived as received:
            response = received.response
        except httpx.LocalProtocolError:
            # h11 quotes the rejected header, bearer token included. Sending the
            # same request again cannot succeed; keep that text out of the chain.
            failure = LocalRequestRejectedError()
        except httpx.TransportError as exc:
            failure = exc
        if isinstance(failure, LocalRequestRejectedError):
            raise failure
        if response is not None:
            # Every response is kept, also an error status or a body cut off midway:
            # the bytes as they came off the wire (still compressed if the server
            # ignored Accept-Encoding: identity); `body` is the decoded entity.
            wire, cut_off, failure = await _read_body(response, closing)
            try:
                if failure is None:
                    body, failure = _decode(wire, response.headers, failure)
            except Exception as exc:
                failure = exc  # Never retried: handled as an unexpected stream failure.
            finally:
                # Keep the wire bytes even if decoding fails in an unexpected way.
                captured.append(
                    _capture(
                        request,
                        response,
                        len(captured),
                        request_context.data_type,
                        body=ReceivedBody(wire, cut_off, failure),
                    )
                )
            if isinstance(failure, asyncio.CancelledError):
                raise failure
        if isinstance(failure, ResponseBodyLimitExceeded):
            raise ResponseBodyTooLargeError()
        if isinstance(failure, ResponseDecodeRejected):
            # Deterministic size/codec failure: never retried.
            raise GoogleHealthError(str(failure), code=failure.code)
        if failure is not None and not isinstance(
            failure, (httpx.TransportError, httpx.DecodingError)
        ):
            # Arbitrary stream failures are not retried and their text, which may
            # hold upstream data, stays out. DecodingError is not a TransportError.
            raise ResponseStreamError() from None
        return response, body, failure

    async def _pace_request(self) -> None:
        interval = self.settings.google_api_min_request_interval_seconds
        if interval <= 0:
            return
        async with self._request_lock:
            loop = asyncio.get_running_loop()
            delay = interval - (loop.time() - self._last_request_started)
            if delay > 0:
                await self.sleep(delay)
            self._last_request_started = loop.time()


# How long a cancelled task (restart, deploy) waits for responses it already received to
# reach the database before it lets the cancellation through.
KEEP_ON_CANCEL_SECONDS = 10.0


async def _finish(store: asyncio.Future, count: int) -> None:
    """Wait within one budget, including repeated cancellation during cleanup.

    asyncio.wait does not cancel the work when its caller is cancelled or times out.
    The deadline stays fixed, so repeated stop signals cannot extend the wait.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + KEEP_ON_CANCEL_SECONDS
    while not store.done():
        remaining = deadline - loop.time()
        if remaining <= 0:
            logger.error(
                "Google Health response cleanup exceeded its cancellation budget",
                extra={"event": "RESPONSE_ARCHIVE_INTERRUPTED", "count": count},
            )
            return
        try:
            await asyncio.wait((store,), timeout=remaining)
        except asyncio.CancelledError:
            continue
    store.result()


# Shared with deletion marking, which must use exactly the bounds Google was sent.
_iso = query_iso


def minute_aligned_range(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    """Keep 60-second rollup buckets stable across overlapping requests."""
    aligned_start = start.replace(second=0, microsecond=0)
    aligned_end = end.replace(second=0, microsecond=0)
    if aligned_end < end:
        aligned_end += timedelta(minutes=1)
    if aligned_end <= aligned_start:
        aligned_end = aligned_start + timedelta(minutes=1)
    return aligned_start, aligned_end


def _retry_after(header: str | None) -> float | None:
    """Parse seconds or an aware HTTP date; malformed/unrepresentable means absent.

    Check finiteness before clamping: max(0, NaN) would turn a bad header into an
    immediate retry.
    """
    return http_retry.retry_after(
        header, now=lambda: datetime.now(UTC), parse_date=parsedate_to_datetime
    )


def _backoff(attempt: int) -> float:
    return http_retry.backoff(attempt, jitter=random.random(), ceiling=MAX_RETRY_DELAY_SECONDS)


def _capture(
    request: httpx.Request,
    response: httpx.Response,
    attempt: int,
    data_type: str | None,
    *,
    body: ReceivedBody,
) -> CapturedResponse:
    content = request.content
    return CapturedResponse(
        fetched_at=datetime.now(UTC),
        source=current_source(),
        context_id=current_context_id(),
        data_type=data_type,
        method=request.method,
        path=request.url.path,
        query=safe_query(request.url.query),
        request_body=content or None,
        attempt=attempt,
        status_code=response.status_code,
        headers=kept_headers(response.headers),
        body=body.wire,
        # The wire bytes are complete unless reading stopped early; `error` also
        # names a body that arrived whole but could not be decoded.
        complete=body.cut_off is None,
        error=type(body.failure).__name__ if body.failure is not None else None,
    )


_JSON_ERRORS = {
    strict_json.INVALID: lambda: GoogleHealthError(
        "Google Health API returned invalid JSON", code=GoogleHealthErrorCode.INVALID_JSON
    ),
    strict_json.NOT_OBJECT: lambda: GoogleHealthError(
        "Google Health API returned a non-object JSON response",
        code=GoogleHealthErrorCode.INVALID_JSON,
    ),
    strict_json.DUPLICATE_KEY: DuplicateResponseKeyError,
    strict_json.CONSTANT: NonStandardResponseConstantError,
    strict_json.ERROR_FIELD: ErrorResponsePayloadError,
}


def _success_payload(response: httpx.Response, body: bytes) -> dict:
    """Reject HTTP failures and partial collections before interpreting their JSON.

    206 is in HTTPX's is_success, but a partial collection must never become
    terminal-page evidence; redirects and 1xx cannot prove the collection either.
    Only the expired page token reads the body of a rejected status; other errors
    expose only the HTTP code. Call after capture: rejection keeps completeness.
    """
    status = response.status_code
    if status == 206 or not response.is_success:
        if status == 400 and _has_error_code(body, "INVALID_PAGE_TOKEN"):
            raise InvalidPageTokenError(
                "Google Health pagination token expired", http_status=status
            )
        raise GoogleHealthError(f"Google Health API returned HTTP {status}", http_status=status)
    try:
        return strict_json.loads_object(body)
    except strict_json.StrictJsonError as exc:
        kind = exc.kind
    # Raise outside the handler, so no parser frame enters the exception chain.
    raise _JSON_ERRORS[kind]()


def _has_error_code(body: bytes, expected: str) -> bool:
    """Require the code in a supported Google error-envelope field.

    A positive match permits discarding a committed continuation, so unrelated
    fields, partial-word message fragments and unknown envelope shapes cannot
    count as evidence. Accepted: the code as a whole word of ``error.message``,
    ``details[].reason`` (google.rpc.ErrorInfo) or
    ``details[].fieldViolations[].reason`` (google.rpc.BadRequest). Keep the
    supported paths shallow: arbitrary JSON nesting must never make
    classification recurse, even when the parser accepts the body.
    """
    try:
        payload = strict_json.loads_object(body, allow_error=True)
    except strict_json.StrictJsonError:
        # Ambiguous, malformed or excessively deep JSON stays an ordinary HTTP
        # failure. The caller has already captured the original response bytes.
        return False
    error = payload.get("error")
    if not isinstance(error, dict):
        return False
    message = error.get("message")
    if isinstance(message, str) and re.search(rf"\b{re.escape(expected)}\b", message):
        return True
    details = error.get("details")
    if not isinstance(details, list):
        return False
    for detail in details:
        if not isinstance(detail, dict):
            continue
        violations = detail.get("fieldViolations")
        items = [detail, *(violations if isinstance(violations, list) else [])]
        if any(isinstance(item, dict) and item.get("reason") == expected for item in items):
            return True
    return False
