"""Providers of normalized rows and the record key of a non-Google row.

A *provider* is where a normalized row came from: ``GOOGLE`` (Google Health, through
``raw_health_data``), ``GARMIN`` (the Garmin Connect Personal API, through
``garmin_raw_records``, or Garmin FIT file imports, through namespaced ``raw_health_data``) or
``APPLE`` (the owner's own Apple Health data, received from Health Auto Export or uploaded as
``export.zip``, through ``apple_raw_records``). ``FIT`` is a file import from another
manufacturer, through that same namespaced archive. Every normalized table carries
``provider`` (migration ``20261107_0025``; rows from before it are ``GOOGLE``) and
``source_record_id``, the provider's own key of the record for a non-Google row.
``google_data_point_id`` stays the
table's unique record key for every row: a Google row holds Google's id (or the synthetic
v1/v2 id), a Garmin API row holds ``garmin_record_key(...)`` and an Apple row
``apple_record_key(...)``, which no Google id can equal (Google ids are resource names,
``synthetic:…`` or ``identity:…``). Readers that join a normalized row to Google data types
in ``raw_health_data`` never find a Garmin or Apple row. FIT uses distinct ``garmin-fit-*``
types and existing identity/supersession rules; its normalized rows carry a ``fit:`` source
key. Google deletion marking and coverage stay Google-only.
"""

from __future__ import annotations

GOOGLE = "GOOGLE"
GARMIN = "GARMIN"
APPLE = "APPLE"
FIT = "FIT"
PROVIDERS = (GOOGLE, GARMIN, FIT, APPLE)
GARMIN_KEY_PREFIX = "garmin:"
APPLE_KEY_PREFIX = "apple:"


def garmin_record_key(kind: str, key: str, *parts: str) -> str:
    """The record key of a Garmin row: the pull kind, the raw record's key (a calendar day
    or an activity id) and, for a record that yields several rows (a day's heart-rate
    samples), the part that tells them apart."""
    return ":".join((f"{GARMIN_KEY_PREFIX}{kind}", key, *parts))


def apple_record_key(channel: str, kind: str, key: str, *parts: str) -> str:
    """The record key of an Apple row: the channel (``export`` or ``hae``), the raw record's
    kind and key (``apple_raw_records``) and, for a record that yields several rows, the part
    that tells them apart. No Google id can equal it."""
    return ":".join((f"{APPLE_KEY_PREFIX}{channel}", kind, key, *parts))


# Synthetic sample data (``healthos demo``, app/services/demo_archive.py): rows of the shared
# normalized tables only, never of a raw archive, since no provider returned them. Every row
# holds ``DEMO`` and a ``demo:<kind>:<key>`` record key, which no Google or Garmin key can equal;
# removing the sample deletes exactly the rows that carry both and refuses when one is mixed.
DEMO = "DEMO"
DEMO_KEY_PREFIX = "demo:"


def demo_record_key(kind: str, key: str, *parts: str) -> str:
    """The record key of a sample row: the kind, the calendar day and, for a day with several
    rows of the kind (heart-rate samples, workouts), the part that tells them apart."""
    return ":".join((f"{DEMO_KEY_PREFIX}{kind}", key, *parts))
