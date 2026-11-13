"""Validated archive query shared by the tool and its interpretation readers."""

from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .health_windows import bounds, input_bounds, interpretation_window
from .interpretation_types import ACTIONS


class ArchiveQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal[
        "inventory",
        "daily",
        "records",
        "coverage",
        "baseline",
        "analyze",
        "sleep",
        "intraday",
        "sessions",
        "physiology",
        "longitudinal",
        "relationship",
        "devices",
    ]
    start_date: date | None = None
    end_date: date | None = None
    data_type: str | None = Field(default=None, max_length=128)
    timezone: str = "UTC"
    offset: int = Field(default=0, ge=0, le=1_000_000)
    limit: int = Field(default=100, ge=1, le=200)

    cursor: str | None = Field(default=None, max_length=2048)
    lookback_days: int = Field(default=56, ge=14, le=90)
    metric: str | None = Field(default=None, max_length=128)
    other_metric: str | None = Field(default=None, max_length=128)
    source: str | None = Field(default=None, max_length=128)
    other_source: str | None = Field(default=None, max_length=128)
    definition: str | None = Field(default=None, max_length=512)
    other_definition: str | None = Field(default=None, max_length=512)
    lags: list[int] = Field(default_factory=lambda: [0], min_length=1, max_length=5)

    @model_validator(mode="after")
    def validate_range(self):
        zone = ZoneInfo(self.timezone)
        if self.action not in ("inventory", "devices"):
            if not self.start_date or not self.end_date:
                raise ValueError("start_date ve end_date gerekli.")
            days = (self.end_date - self.start_date).days + 1
            maximum = (
                366
                if self.action in {"daily", "coverage"}
                else 180
                if self.action == "relationship"
                else 90
                if self.action in ACTIONS
                else 31
            )
            if not 1 <= days <= maximum:
                raise ValueError(f"Bir sorgu en fazla {maximum} gün olabilir.")
            if self.end_date > datetime.now(zone).date():
                raise ValueError("Gelecek tarih seçilemez.")
            try:
                if self.action in ACTIONS:
                    first, last = interpretation_window(
                        self.action, self.start_date, self.end_date, self.lookback_days
                    )
                    input_bounds(first, last, self.timezone)
                else:
                    bounds(self.start_date, self.timezone)
                    bounds(self.end_date, self.timezone)
            except OverflowError as exc:
                raise ValueError(
                    "Sorgunun okuma penceresi temsil edilebilir tarihleri aşıyor."
                ) from exc
        if self.action == "records" and not self.data_type:
            raise ValueError("records sorgusunda data_type gerekli.")
        if self.action in ACTIONS:
            metrics = {"steps", "sleep_seconds", "resting_bpm", "hrv_ms", "spo2"}
            if self.offset or (self.metric and self.metric not in metrics):
                raise ValueError("Yorum sorgusunda sürüm 2 imleci ve desteklenen metric kullan.")
            if self.action == "relationship" and (
                not self.metric or self.other_metric not in metrics
            ):
                raise ValueError("relationship iki günlük metric gerektirir.")
            if len(set(self.lags)) != len(self.lags) or any(abs(lag) > 14 for lag in self.lags):
                raise ValueError("Gecikmeler önceden belirlenmiş, tekil ve -14–14 gün olmalı.")
        return self

    def date_range(self) -> tuple[date, date]:
        """The validated (start_date, end_date) of a daily or records query."""
        if self.start_date is None or self.end_date is None:
            raise ValueError("start_date ve end_date gerekli.")
        return self.start_date, self.end_date
