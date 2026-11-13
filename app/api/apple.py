"""Apple Health: the Health Auto Export receiver, the export upload and the import status.

* ``POST /webhooks/apple-health`` (APPLE_RECEIVER_PATH): the Health Auto Export iOS app's
  REST API automation. Off unless APPLE_RECEIVER_ENABLED (404); its own bearer token, read
  from APPLE_RECEIVER_TOKEN_FILE, never the UI token (the path is exempt from the UI login).
* ``POST /api/apple/import``: the Health app's ``export.zip``/``export.xml`` as the raw
  request body, streamed into the archive (loopback, UI token and the job-request CSRF check).
* ``GET /api/apple/status``: receiver configuration, the running import, the last imports and
  the Health sources seen (no secrets, no record values).
"""

from __future__ import annotations

import hmac
import logging
import re
from typing import Any
from urllib.parse import unquote

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import bounded_body, require_budget, require_job_request
from app.api.deps import settings as settings_dep
from app.core.apple_paths import APPLE_IMPORT_PATH, APPLE_RECEIVER_PATH, receiver_token
from app.core.config import Settings
from app.core.logging import event
from app.core.request_limits import RequestBudget
from app.schemas.apple import AppleImportRow, AppleReceiverResult, AppleReceiverStatus, AppleStatus
from app.services.providers import apple_import

logger = logging.getLogger(__name__)
router = APIRouter(tags=["apple"])

IMPORT_TYPES = frozenset(
    {"application/zip", "application/xml", "text/xml", "application/octet-stream"}
)
FILENAME_HEADER = "x-apple-export-filename"


class AppleLimits:
    """The receiver's request budgets (process-local, like WriteLimits): requests that carry
    the right token spend ``authorized``, all others ``unauthorized``, so a flood without the
    token cannot starve the phone's deliveries."""

    def __init__(self, settings: Settings) -> None:
        self.authorized = RequestBudget(settings.apple_receiver_requests_per_minute)
        self.unauthorized = RequestBudget(settings.apple_receiver_requests_per_minute)


def sessions_dep(request: Request) -> async_sessionmaker:
    return request.app.state.apple_sessions


def apple_limits(request: Request) -> AppleLimits:
    limits = getattr(request.app.state, "apple_limits", None)
    if limits is None:
        limits = request.app.state.apple_limits = AppleLimits(request.app.state.settings)
    return limits


def _bearer(request: Request) -> str | None:
    values = request.headers.getlist("authorization")
    if len(values) != 1:
        return None
    scheme, _, credentials = values[0].partition(" ")
    return credentials.strip() if scheme.lower() == "bearer" else None


@router.post(APPLE_RECEIVER_PATH, include_in_schema=False, response_model=AppleReceiverResult)
async def apple_health_receiver(
    request: Request,
    settings: Settings = Depends(settings_dep),
    sessions: async_sessionmaker = Depends(sessions_dep),
) -> JSONResponse:
    if not settings.apple_receiver_enabled:
        raise HTTPException(404, "Not Found")
    token = receiver_token(settings)
    candidate = _bearer(request)
    authorized = (
        token is not None
        and candidate is not None
        and hmac.compare_digest(candidate.encode("utf-8"), token.encode("utf-8"))
    )
    limits = apple_limits(request)
    # Before any other work: refused floods never touch the disk or the database.
    require_budget(limits.authorized if authorized else limits.unauthorized)
    if token is None:
        event(logger, "APPLE_RECEIVER_UNCONFIGURED")
        raise HTTPException(503, "receiver token not configured")
    if not authorized:
        event(logger, "APPLE_RECEIVER_REJECTED", reason="authorization")
        raise HTTPException(401, "Invalid receiver authorization")
    try:
        body = await bounded_body(
            request, settings.apple_receiver_max_body_bytes, "Payload is too large"
        )
    except HTTPException as exc:
        event(logger, "APPLE_RECEIVER_REJECTED", reason="body", status=exc.status_code)
        raise
    try:
        result = await apple_import.receive_hae(sessions, body, request.headers)
    except apple_import.ImportRejected as exc:
        raise HTTPException(400, "Invalid Health Auto Export payload") from exc
    except SQLAlchemyError as exc:
        event(logger, "APPLE_RECEIVER_FAILED", error_type=type(exc).__name__)
        raise HTTPException(503, "Archive unavailable") from exc
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


def _filename(request: Request) -> str | None:
    value = request.headers.get(FILENAME_HEADER)
    if not value:
        return None
    # Percent-encoded by the UI (header values are Latin-1); only the base name, printable,
    # 200 characters at most.
    value = unquote(value)
    name = re.sub(r"[\x00-\x1f\x7f]", "", value.replace("\\", "/").rsplit("/", 1)[-1]).strip()
    return name[:200] or None


@router.post(
    APPLE_IMPORT_PATH,
    dependencies=[Depends(require_job_request)],
    response_model=AppleImportRow,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                kind: {"schema": {"type": "string", "format": "binary"}}
                for kind in sorted(IMPORT_TYPES)
            },
        }
    },
)
async def apple_import_upload(
    request: Request,
    settings: Settings = Depends(settings_dep),
    sessions: async_sessionmaker = Depends(sessions_dep),
) -> JSONResponse:
    """Import the Health app's export.zip or export.xml sent as the raw request body."""
    kind = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if kind not in IMPORT_TYPES:
        raise HTTPException(415, "Yalnız export.zip veya export.xml yüklenebilir.")
    limit = settings.apple_import_max_upload_bytes
    lengths = request.headers.getlist("content-length")
    if lengths:
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,20}", lengths[0]):
            raise HTTPException(400, "Invalid Content-Length")
        if int(lengths[0]) > limit:
            raise HTTPException(413, f"Dosya {limit // (1024 * 1024)} MB sınırını aşıyor.")
    try:
        result = await apple_import.import_stream(
            sessions,
            request.stream(),
            origin=apple_import.UPLOAD,
            filename=_filename(request),
            max_bytes=limit,
            reader=apple_import.export_reader(settings.apple_import_max_uncompressed),
        )
    except apple_import.AppleImportError as exc:
        raise HTTPException(exc.status, exc.message) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(503, "Arşive yazılamadı; biraz sonra yeniden dene.") from exc
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


async def _sources(session: AsyncSession) -> Any:
    try:
        from app.services.providers.apple_readers import sources
    except ImportError:
        return None
    return await sources(session)


@router.get("/api/apple/status", response_model=AppleStatus)
async def apple_status(
    response: Response,
    settings: Settings = Depends(settings_dep),
    sessions: async_sessionmaker = Depends(sessions_dep),
) -> AppleStatus:
    """Receiver configuration, the running import, the last imports and the Health sources."""
    response.headers["Cache-Control"] = "no-store"
    receiver: AppleReceiverStatus = {
        "enabled": settings.apple_receiver_enabled,
        "token_file_present": receiver_token(settings) is not None,
        "endpoint_configured": settings.apple_receiver_endpoint_uri is not None,
        "path": APPLE_RECEIVER_PATH,
    }
    try:
        async with sessions() as session:
            imports = await apple_import.recent_imports(session)
            sources = await _sources(session)
    except SQLAlchemyError as exc:
        raise HTTPException(503, "Apple status unavailable") from exc
    return {
        "receiver": receiver,
        "running": apple_import.running(),
        "imports": imports,
        "sources": sources,
    }
