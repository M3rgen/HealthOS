"""Binary file upload only; independent of the Garmin Connect API key and pull jobs."""

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import bounded_body, require_budget, require_job_request
from app.core.fit import MAX_FIT_BYTES
from app.db.session import get_session
from app.services.providers.fit_decode import FitError
from app.services.providers.fit_import import import_fit_bytes

router = APIRouter(prefix="/api/imports", tags=["imports"])


@router.post("/garmin-fit", dependencies=[Depends(require_job_request)])
async def upload_fit(
    request: Request, response: Response, session: AsyncSession = Depends(get_session)
) -> dict:
    """POST a raw .fit body (application/octet-stream or application/vnd.ant.fit).

    UI authentication, explicit request marker and the shared write-rate budget apply.
    Multipart and ZIP are intentionally unsupported; filenames are neither used nor stored.
    Imports work in archive-only mode and never require a Garmin API key.
    """
    require_budget(request.app.state.write_limits.manual)
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type not in {"application/octet-stream", "application/vnd.ant.fit"}:
        raise HTTPException(415, "FIT dosyasını ikili istek gövdesi olarak yükleyin.")
    body = await bounded_body(request, MAX_FIT_BYTES, "FIT dosyası çok büyük.")
    try:
        result = await import_fit_bytes(session, body)
    except FitError as exc:
        raise HTTPException(422, str(exc)) from exc
    await session.commit()
    response.headers["Cache-Control"] = "no-store"
    return asdict(result)
