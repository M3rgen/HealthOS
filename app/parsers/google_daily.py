"""Google daily SpO₂ aliases in dashboard order (maintainer, 2026-11-13)."""

from decimal import Decimal

from app.parsers.common import number

DAILY_SPO2_FIELDS = ("averagePercentage", "percentage", "average")


def daily_spo2(payload: dict) -> tuple[str | None, Decimal | None]:
    """Select before validating: a rejected number cannot expose another alias.

    Missing and non-numeric fields fall through. If no number exists, retain
    the first supplied field as canonical missing evidence. ``spo2`` is a legacy
    sample-normalizer alias, not a field produced by the Google daily format.
    """
    for field in DAILY_SPO2_FIELDS:
        value = number(payload.get(field))
        if value is not None:
            return field, value
    return next((field for field in DAILY_SPO2_FIELDS if field in payload), None), None
