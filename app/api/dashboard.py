import csv
import io
import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response
from pydantic import BaseModel, field_validator, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ManualWriteRoute, require_job_request
from app.db.session import get_session
from app.schemas.dashboard import Dashboard, History, Measurement, MeasurementFlag, Workout
from app.services.archive.manual_flags import REASON_LENGTH, TEST_FLAG
from app.services.dashboard_views.dashboard import dashboard_data
from app.services.dashboard_views.dashboard_history import HistoryPageNotFound, history_data
from app.services.dashboard_views.dashboard_workouts import create_workout, list_workouts

# Compatibility for callers of the original API helper.
from app.services.dashboard_views.dashboard_workouts import place_workouts as place_workouts
from app.services.readers.dashboard_manual import (
    MeasurementNotFound,
    create_measurement,
    list_measurements,
    set_measurement_flag,
)
from app.services.readers.dashboard_queries import DashboardQuery
from app.services.readers.dashboard_window import DateWindowError
from app.services.readers.manual_validation import (
    valid_manual_date,
    valid_manual_number,
    valid_manual_text,
    validate_measurement,
    validate_workout,
)


class DashboardRoute(ManualWriteRoute):
    """Reject invalid inputs without echoing values that may not be JSON-serializable."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def window_handler(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError as exc:
                raise HTTPException(422, "Girdi geçersiz. Lütfen değerleri kontrol edin.") from exc
            except DateWindowError as exc:
                raise HTTPException(422, str(exc)) from exc

        return window_handler


router = APIRouter(prefix="/api/dashboard", tags=["dashboard"], route_class=DashboardRoute)


class WorkoutInput(BaseModel):
    exercise_type: str
    start_time: datetime
    end_time: datetime
    calories_kcal: Decimal | None = None
    distance_meters: Decimal | None = None

    @field_validator("exercise_type")
    @classmethod
    def valid_type(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > 128:
            raise ValueError("Spor türü 1–128 karakter olmalı.")
        return valid_manual_text(value)

    @field_validator("start_time", "end_time")
    @classmethod
    def valid_date(cls, value: datetime) -> datetime:
        return valid_manual_date(value)

    @field_validator("calories_kcal", "distance_meters")
    @classmethod
    def valid_number(cls, value: Decimal | None) -> Decimal | None:
        return valid_manual_number(value)

    @model_validator(mode="after")
    def valid_workout(self):
        validate_workout(self.model_dump(), now=datetime.now(UTC))
        return self


@router.post(
    "/workouts",
    status_code=201,
    dependencies=[Depends(require_job_request)],
    response_model=Workout,
)
async def add_workout(body: WorkoutInput, session: AsyncSession = Depends(get_session)) -> Workout:
    result = await create_workout(session, body.model_dump())
    await session.commit()
    return result


@router.get("/workouts", response_model=list[Workout])
async def workouts(
    month: date | None = None,
    timezone: str = "UTC",
    session: AsyncSession = Depends(get_session),
) -> list[Workout]:
    zone = check_zone(timezone)
    current = datetime.now(zone).date()
    first = (month or current).replace(day=1)
    if first > current:
        raise HTTPException(422, "Gelecek ay seçilemez.")
    return await list_workouts(session, first, zone)


def check_zone(timezone: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise HTTPException(422, "Geçerli bir saat dilimi seçin.") from exc


@router.get("", response_model=Dashboard)
async def dashboard(
    days: int = Query(7, ge=1, le=31),
    end_date: date | None = None,
    timezone: str = "UTC",
    session: AsyncSession = Depends(get_session),
) -> Dashboard:
    zone = check_zone(timezone)
    end_date = end_date or datetime.now(zone).date()
    if end_date > datetime.now(zone).date():
        raise HTTPException(422, "Gelecek tarih seçilemez.")
    return await dashboard_data(session, query=DashboardQuery(end_date, days, timezone))


@router.get("/history", response_model=History)
async def metric_history(
    metric: Literal["steps", "sleep", "heart"],
    page: int = Query(1, ge=1),
    timezone: str = "UTC",
    session: AsyncSession = Depends(get_session),
) -> History:
    """Thirty civil days per page, newest first, through the full valid archive."""
    zone = check_zone(timezone)
    today = datetime.now(zone).date()
    try:
        return await history_data(session, metric, page, timezone, today)
    except HistoryPageNotFound as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/export.csv")
async def export(
    days: int = Query(7, ge=1, le=31),
    end_date: date | None = None,
    timezone: str = "UTC",
    session: AsyncSession = Depends(get_session),
) -> Response:
    data = await dashboard(days, end_date, timezone, session)
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(
        ["date", "steps", "sleep_seconds", "resting_bpm", "hrv_ms", "spo2", "status", "timezone"]
    )
    for row in data["days"]:
        writer.writerow(
            [
                row[key] if row[key] is not None else ""
                for key in (
                    "date",
                    "steps",
                    "sleep_seconds",
                    "resting_bpm",
                    "hrv_ms",
                    "spo2",
                    "status",
                )
            ]
            + [timezone]
        )
    return Response(
        "\ufeff" + buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="healthos-{data["end_date"]}.csv"',
            "Cache-Control": "no-store",
        },
    )


class MeasurementInput(BaseModel):
    metric: Literal["height", "weight", "body_fat", "muscle"]
    value: Decimal
    measured_at: datetime

    @field_validator("measured_at")
    @classmethod
    def valid_date(cls, value: datetime) -> datetime:
        value = valid_manual_date(value)
        return value

    @model_validator(mode="after")
    def valid_value(self):
        validate_measurement(self.model_dump(), now=datetime.now(UTC))
        return self


@router.post(
    "/measurements",
    status_code=201,
    dependencies=[Depends(require_job_request)],
    response_model=Measurement,
)
async def add_measurement(
    body: MeasurementInput, session: AsyncSession = Depends(get_session)
) -> Measurement:
    result = await create_measurement(session, body.model_dump())
    await session.commit()
    return result


@router.get("/measurements", response_model=list[Measurement])
async def measurements(session: AsyncSession = Depends(get_session)) -> list[Measurement]:
    return await list_measurements(session)


class FlagInput(BaseModel):
    flag: Literal["test"] = TEST_FLAG
    reason: str | None = None

    @field_validator("reason")
    @classmethod
    def valid_reason(cls, value: str | None) -> str | None:
        value = value.strip() if value else None
        if value and len(value) > REASON_LENGTH:
            raise ValueError(f"Açıklama en fazla {REASON_LENGTH} karakter olmalı.")
        return valid_manual_text(value) if value else None


async def _flag(
    session: AsyncSession,
    measurement_id: uuid.UUID,
    body: FlagInput | None,
    flagged: bool,
    response: Response,
) -> MeasurementFlag:
    body = body or FlagInput()
    try:
        result = await set_measurement_flag(
            session, measurement_id, body.flag, flagged, body.reason
        )
    except MeasurementNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    await session.commit()
    # Already in that state: nothing was added.
    response.status_code = 201 if result["changed"] else 200
    return result


@router.post(
    "/measurements/{measurement_id}/flag",
    dependencies=[Depends(require_job_request)],
    response_model=MeasurementFlag,
)
async def flag_measurement(
    measurement_id: uuid.UUID,
    response: Response,
    body: FlagInput | None = None,
    session: AsyncSession = Depends(get_session),
) -> MeasurementFlag:
    """Flag a measurement (``test``: a test entry). Appends an event; the entry is unchanged."""
    return await _flag(session, measurement_id, body, True, response)


@router.post(
    "/measurements/{measurement_id}/unflag",
    dependencies=[Depends(require_job_request)],
    response_model=MeasurementFlag,
)
async def unflag_measurement(
    measurement_id: uuid.UUID,
    response: Response,
    body: FlagInput | None = None,
    session: AsyncSession = Depends(get_session),
) -> MeasurementFlag:
    """Clear a flag. Appends an event too: the earlier decision stays as history."""
    return await _flag(session, measurement_id, body, False, response)
