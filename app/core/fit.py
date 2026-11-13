"""Namespaced file-import records; never part of Google's fetched type registry.

``garmin-fit-*`` names are a stable internal namespace, not a manufacturer label;
the dataSource and provider/source code identify Garmin or another FIT manufacturer.
"""

from app.core.records import RecordType

FIT_FILE = RecordType("garmin-fit-file")
FIT_EXERCISE = RecordType("garmin-fit-exercise")
FIT_HEART_RATE = RecordType("garmin-fit-heart-rate")
FIT_NORMALIZED = {FIT_EXERCISE.name: "exercise", FIT_HEART_RATE.name: "heart-rate"}
FIT_SOURCE = "garmin-fit"
FIT_RECORD_PREFIX = "fit:"
MAX_FIT_BYTES = 8 * 1024 * 1024
MAX_FIT_MESSAGES = 100_000
MAX_FIT_RECORDS = 50_000
