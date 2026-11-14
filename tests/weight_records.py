"""Synthetic weight payloads supported by the production ingestion parser."""

from derived_records import T0, point


def weight(name="one", grams=70000, at=T0, **fields):
    return point(
        "weight",
        name,
        {"sampleTime": {"physicalTime": at.isoformat()}, "weightGrams": grams, **fields},
    )


NEGATIVE_GRAMS = [-70000, "-70000", -0.001, "-0.001", "-5e-324", "-1e-999999"]

INVALID_FIELDS = [
    {"weightGrams": None},
    {"weightGrams": "broken"},
    {"weightGrams": True},
    {"sampleTime": None},
    {"sampleTime": {"physicalTime": "broken"}},
    {"sampleTime": {"physicalTime": False}},
    {},  # the grams field is removed by the correction helper
]


def correction(at, fields):
    record = weight(at=at)
    if fields:
        record["weight"].update(fields)
    else:
        record["weight"].pop("weightGrams")
    return record
