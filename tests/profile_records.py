"""Synthetic Google body measurements sent through production ingestion."""

from derived_records import BAND, T0, point

CASES = [
    ("weight", "weight", "weightGrams", 70000, 70),
    ("height", "height", "heightMillimeters", 1700, 170),
    ("body-fat", "body_fat", "percentage", 20, 20),
]


def measurement(kind, field, name, value, at=T0, source=BAND):
    return point(kind, name, {"sampleTime": {"physicalTime": at.isoformat()}, field: value}, source)
