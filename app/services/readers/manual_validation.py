"""Manual entry rules shared by service writes and request validation."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any


def valid_manual_text(value: str) -> str:
    if "\x00" in value:
        raise ValueError("Metin geçersiz karakter içeriyor.")
    # PostgreSQL text and the response encoder both require valid UTF-8.
    value.encode("utf-8")
    return value


def valid_manual_date(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Tarih saat dilimi içermeli.")
    try:
        return value.astimezone(UTC)
    except OverflowError as exc:
        raise ValueError("Tarih UTC aralığında olmalı.") from exc


def valid_manual_number(value: Decimal | None) -> Decimal | None:
    if value is not None:
        exponent = value.as_tuple().exponent
        # asyncpg's numeric wire format has bounded scale and weight, even for zero.
        if not isinstance(exponent, int) or not -16383 <= exponent <= 131071:
            raise ValueError("Sayısal değer depolanabilir aralıkta olmalı.")
    return value


def validate_workout(values: dict[str, Any], *, now: datetime | None = None) -> None:
    exercise_type = values["exercise_type"].strip()
    if not exercise_type or len(exercise_type) > 128:
        raise ValueError("Spor türü 1–128 karakter olmalı.")
    valid_manual_text(exercise_type)
    start, end = (valid_manual_date(values[key]) for key in ("start_time", "end_time"))
    if end <= start or end > (now or datetime.now(UTC)):
        raise ValueError("Bitiş başlangıçtan sonra olmalı ve gelecekte olmamalı.")
    for key, maximum in (("calories_kcal", 100000), ("distance_meters", 10000000)):
        value = valid_manual_number(values.get(key))
        if value is not None and (not value.is_finite() or not 0 <= value <= maximum):
            raise ValueError("Kalori ve mesafe geçerli, negatif olmayan değerler olmalı.")


def validate_measurement(values: dict[str, Any], *, now: datetime | None = None) -> None:
    measured_at = valid_manual_date(values["measured_at"])
    if measured_at > (now or datetime.now(UTC)):
        raise ValueError("Ölçüm tarihi saat dilimi içermeli ve gelecekte olmamalı.")
    value = valid_manual_number(values["value"])
    bounds = {"height": (30, 260), "weight": (1, 500), "body_fat": (0, 100), "muscle": (0, 100)}
    if values["metric"] not in bounds:
        raise ValueError("Ölçüm türü geçersiz.")
    low, high = bounds[values["metric"]]
    if value is None or not value.is_finite() or not low <= value <= high:
        raise ValueError(f"Ölçüm {low}–{high} aralığında olmalı.")
