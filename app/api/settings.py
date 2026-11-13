"""Bounded request parsing and HTTP responses for shared dashboard settings."""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import bounded_body, require_job_request
from app.db.session import get_session
from app.schemas.settings import Appearance, Display
from app.services.operations import settings
from app.services.operations.settings import AppearanceSelection, DisplayPreferencesBody

router = APIRouter(prefix="/api/settings", tags=["settings"])

# The payload is three short ids; anything near this size is not a real request.
MAX_BODY_BYTES = 4 * 1024


async def _parse[T: BaseModel](request: Request, model: type[T], message: str) -> T:
    raw = await bounded_body(request, MAX_BODY_BYTES, message)
    try:
        return model.model_validate_json(raw)
    except ValidationError as error:
        raise HTTPException(
            422, jsonable_encoder(error.errors(include_url=False, include_context=False))
        ) from error


@router.get("/appearance", response_model=Appearance)
async def get_appearance(session: AsyncSession = Depends(get_session)) -> Appearance:
    return await settings.get_appearance(session)


@router.put(
    "/appearance",
    dependencies=[Depends(require_job_request)],
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": AppearanceSelection.model_json_schema()}},
        }
    },
    response_model=Appearance,
)
async def put_appearance(
    request: Request, session: AsyncSession = Depends(get_session)
) -> Appearance:
    """Replace the shared selection (last write wins)."""
    body = await _parse(request, AppearanceSelection, "Appearance payload is too large")
    result = await settings.put_appearance(session, body)
    await session.commit()
    return result


@router.get("/display", response_model=Display)
async def get_display(session: AsyncSession = Depends(get_session)) -> Display:
    return await settings.get_display(session)


@router.put(
    "/display",
    dependencies=[Depends(require_job_request)],
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": DisplayPreferencesBody.model_json_schema()}},
        }
    },
    response_model=Display,
)
async def put_display(request: Request, session: AsyncSession = Depends(get_session)) -> Display:
    """Replace the shared display preferences (last write wins)."""
    body = await _parse(request, DisplayPreferencesBody, "Display payload is too large")
    try:
        result = await settings.put_display(session, body)
        await session.commit()
        return result
    except settings.NewerDisplaySchema as exc:
        raise HTTPException(409, str(exc)) from exc
