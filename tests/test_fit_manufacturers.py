"""Manufacturer labels, legacy identities and precedence using synthetic FIT bytes only."""

import json
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fit_factory import START, activity

from app.core.config import Settings
from app.core.fit import FIT_EXERCISE
from app.core.providers import FIT, PROVIDERS
from app.parsers.common import payload_hash
from app.services.archive.normalization import normalize
from app.services.dashboard_views.dashboard_workouts import list_workouts
from app.services.identities.source_precedence import (
    ACTIVITY,
    HEALTH,
    Observation,
    Policy,
    resolve,
    resolve_samples,
)
from app.services.providers.fit_import import import_fit
from app.services.providers.fit_records import parse_fit
from app.services.providers.ingestion_readers import FIT_ARCHIVE


def test_garmin_parse_output_matches_bytes_captured_before_manufacturer_support():
    parsed = parse_fit(activity())
    snapshot = {
        "sha256": parsed.sha256,
        "file": parsed.file,
        "records": {spec.name: rows for spec, rows in parsed.records.items()},
    }
    actual = (json.dumps(snapshot, sort_keys=True, ensure_ascii=False) + "\n").encode()
    assert actual == (Path(__file__).parent / "fixtures" / "fit-garmin-parsed.json").read_bytes()


@pytest.mark.parametrize("manufacturer", [2, 32, 255])
@pytest.mark.parametrize("sport, expected", [(1, "RUNNING"), (222, "FIT_SPORT_222")])
def test_other_manufacturers_preserve_standard_fields_without_garmin_labels(
    manufacturer, sport, expected
):
    parsed = parse_fit(activity(manufacturer=manufacturer, sport=sport))
    garmin = parse_fit(activity(sport=sport))
    assert FIT in PROVIDERS
    for spec, records in parsed.records.items():
        assert records[0][spec.json_key] == garmin.records[spec][0][spec.json_key]
        for record in records:
            source = record["dataSource"]
            assert source["platform"] == FIT
            assert source["device"] == {
                "manufacturer": f"FIT manufacturer {manufacturer}",
                "displayName": "FIT dosyası",
            }
            assert source["application"]["packageName"] == "fit." + payload_hash(
                {"manufacturer": manufacturer, "product": 123, "serial": 4242}
            )
            assert source != garmin.records[spec][0]["dataSource"]
            normalized = normalize(spec, record)
            assert normalized.values["provider"] == FIT
            assert normalized.values["source_record_id"].startswith("fit:")
            # garmin-fit-* record-type names remain the internal archive namespace.
            assert "garmin" not in json.dumps(source).lower()
            assert "garmin" not in json.dumps(normalized.values, default=str).lower()
    exercise = parsed.records[FIT_EXERCISE][0][FIT_EXERCISE.json_key]
    assert exercise["exerciseType"] == expected
    assert exercise["interval"]["startTime"] == START.isoformat()


@pytest.mark.parametrize("source", [None, "", []])
def test_legacy_normalization_without_a_source_object_keeps_its_provider(source):
    record = parse_fit(activity()).records[FIT_EXERCISE][0]
    record["dataSource"] = source
    assert normalize(FIT_EXERCISE, record).values["provider"] == "GARMIN"


@pytest.mark.parametrize("serial", [0, 4242])
def test_other_manufacturer_device_identity_is_stable_across_binary_encodings(serial):
    first = parse_fit(activity(manufacturer=255, serial=serial))
    equivalent = parse_fit(activity(manufacturer=255, serial=serial, endian=">", developer=True))
    assert first.records == equivalent.records


@pytest.mark.parametrize("order", ["GARMIN,FITBIT,HEALTH_KIT", "FIT,MANUAL,GOOGLE,DEMO,GARMIN"])
def test_fit_follows_all_devices_in_both_policies_without_reordering_manual_or_demo(order):
    settings = Settings(
        _env_file=None, source_precedence_activity=order, source_precedence_health=order
    )
    policy = Policy.from_settings(settings)
    previous_order = tuple(code for code in order.split(",") if code != FIT)
    previous = Policy(previous_order, previous_order)
    for kind in (ACTIVITY, HEALTH):
        for source in (
            "GARMIN",
            "FITBIT",
            "HEALTH_KIT",
            "HEALTH_CONNECT",
            "GOOGLE_FIT",
            "GOOGLE",
            "WHOOP",
        ):
            assert policy.rank(source, kind) < policy.rank(FIT, kind)
        for source in ("MANUAL", "DEMO"):
            assert policy.rank(source, kind) == previous.rank(source, kind)
            assert policy.priority(source, kind) == previous.priority(source, kind)
    for metric in ("workouts", "heart_rate"):
        result = resolve(metric, [Observation(FIT, 120), Observation("GOOGLE", 110)], policy)
        assert result.selected.source == "GOOGLE"
        assert result.alternatives[0].source == FIT
    kept, _ = resolve_samples({FIT: [(START, 120)], "GARMIN": [(START, 110)]}, policy=policy)
    assert kept == [(START, 110, "GARMIN")]


async def test_overlapping_garmin_and_other_fit_activity_selects_garmin(sessions):
    async with sessions.begin() as session:
        await import_fit(session, parse_fit(activity(manufacturer=255)))
        await import_fit(session, parse_fit(activity()))
    async with sessions() as session:
        rows = await list_workouts(session, START.date(), ZoneInfo("UTC"))
        assert len(rows) == 2
        by_source = {row["source_code"]: row for row in rows}
        assert by_source["GARMIN"]["priority"] == "primary"
        assert by_source[FIT]["priority"] == "secondary"
        assert by_source[FIT]["group"] == by_source["GARMIN"]["id"]
        assert by_source["GARMIN"]["alternatives"][0]["source_code"] == FIT
        assert by_source[FIT]["source"] == "FIT dosyası"
        heart = await FIT_ARCHIVE.heart_rate(session, START, START + timedelta(minutes=10), True)
        assert heart.samples == [(START, 120, FIT), (START + timedelta(seconds=1), 121, FIT)]
