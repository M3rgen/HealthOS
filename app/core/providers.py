"""Providers of normalized rows and the record key of a non-Google row.

A *provider* is where a normalized row came from: ``GOOGLE`` (Google Health, through
``raw_health_data``) or ``GARMIN`` (the Garmin Connect Personal API, through
``garmin_raw_records``). Every normalized table carries ``provider`` (migration
``20261107_0025``; rows from before it are ``GOOGLE``) and ``source_record_id``, the
provider's own key of the record for a non-Google row. ``google_data_point_id`` stays the
table's unique record key for every row: a Google row holds Google's id (or the synthetic
v1/v2 id), a Garmin row holds ``garmin_record_key(...)``, which no Google id can equal
(Google ids are resource names, ``synthetic:…`` or ``identity:…``). Readers that join a
normalized row to ``raw_health_data`` therefore never find a Garmin row, which is what keeps
the Google readers (eligibility, coverage, deletion marking, derived days) Google-only.
"""

from __future__ import annotations

GOOGLE = "GOOGLE"
GARMIN = "GARMIN"
PROVIDERS = (GOOGLE, GARMIN)
GARMIN_KEY_PREFIX = "garmin:"


def garmin_record_key(kind: str, key: str, *parts: str) -> str:
    """The record key of a Garmin row: the pull kind, the raw record's key (a calendar day
    or an activity id) and, for a record that yields several rows (a day's heart-rate
    samples), the part that tells them apart."""
    return ":".join((f"{GARMIN_KEY_PREFIX}{kind}", key, *parts))
