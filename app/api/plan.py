"""Authenticated plan reads and bounded, same-origin state replacements."""

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import bounded_body, require_job_request
from app.db.session import get_session
from app.schemas.plan import PlanStateBody, PlanStateView, PlanView
from app.services.operations import plans
from app.services.operations.plan_validation import InvalidPlan, parse_content

router = APIRouter(prefix="/api/plan", tags=["plan"])
MAX_STATE_BYTES = 128 * 1024


@router.get("", response_model=PlanView)
async def get_plan(response: Response, session: AsyncSession = Depends(get_session)) -> PlanView:
    response.headers["Cache-Control"] = "no-store"
    return await plans.get_plan(session)


@router.put(
    "/state",
    dependencies=[Depends(require_job_request)],
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": PlanStateBody.model_json_schema()}},
        }
    },
    response_model=PlanStateView,
)
async def put_state(
    request: Request, response: Response, session: AsyncSession = Depends(get_session)
) -> PlanStateView:
    response.headers["Cache-Control"] = "no-store"
    raw = await bounded_body(request, MAX_STATE_BYTES, "İstek çok büyük / Request too large")
    try:
        _, body = parse_content(raw, PlanStateBody)
        result = await plans.put_state(session, body)
    except InvalidPlan as exc:
        raise HTTPException(
            422, {"path": exc.path, "rule": exc.rule}, headers={"Cache-Control": "no-store"}
        ) from None
    await session.commit()
    return result
