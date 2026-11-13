"""Cihazlarım: devices built from sightings and live chain links, their status, the Google
pairing and the HTTP/Umay reads. Synthetic sources only; no account data."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from identity_records import fitbit, phone
from sqlalchemy.ext.asyncio import async_sessionmaker

from app import main
from app.core.config import Settings
from app.db.session import get_session
from app.google_health.client import GoogleHealthError
from app.google_health.oauth import OAuthError
from app.parsers.identity import source_fingerprint, source_label, stable_class
from app.services.dashboard_views import devices as devices_service
from app.services.dashboard_views.devices import (
    CONNECTED,
    DELAYED,
    DISCONNECTED,
    Thresholds,
    attach_google,
    build_devices,
    device_report,
)
from app.services.identities import source_chain_repository as repository
from app.services.identities.identity_options import ChainEvidence
from app.services.identities.source_chain_evidence import Span
from app.services.identities.source_chain_model import Chains
from app.services.umay import codex_health
from app.services.umay.codex_health import ArchiveQuery, read_health

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
OLD = fitbit("Google Fitbit Air")
NEW = fitbit("Fitbit Air")
PHONE = phone("com.example.health.0A1B2C3D-4E5F-6071-8293-A4B5C6D7E8F9")
SCALE = {
    "platform": "FITBIT",
    "recordingMethod": "MANUALLY_ENTERED",
    "device": {"displayName": "Aria Example", "formFactor": "SCALE"},
}


def span(source: dict, first: datetime, last: datetime, newest: datetime | None) -> Span:
    return Span(first, last, stable_class(source), source_label(source), True, source, newest)


def chains(*spans: tuple[dict, Span], links: list[tuple[dict, dict]] = ()) -> Chains:
    found = Chains()
    for source, value in spans:
        found.set_span(source_fingerprint(source), value)
    for old, new in links:
        found.add(source_fingerprint(old), source_fingerprint(new))
    return found


def watch_chains() -> dict[str, Chains]:
    """A watch renamed by Google (one live link in heart-rate), seen under its new name in
    steps too; a phone that stopped three days ago; a scale silent for forty days."""
    return {
        "heart-rate": chains(
            (
                OLD,
                span(
                    OLD,
                    NOW - timedelta(days=400),
                    NOW - timedelta(days=30),
                    NOW - timedelta(days=31),
                ),
            ),
            (
                NEW,
                span(
                    NEW,
                    NOW - timedelta(days=30),
                    NOW - timedelta(hours=1),
                    NOW - timedelta(hours=2),
                ),
            ),
            links=[(OLD, NEW)],
        ),
        "steps": chains(
            (NEW, span(NEW, NOW - timedelta(days=29), NOW, NOW - timedelta(minutes=20))),
            (
                PHONE,
                span(
                    PHONE,
                    NOW - timedelta(days=200),
                    NOW - timedelta(days=1),
                    NOW - timedelta(days=3),
                ),
            ),
        ),
        "weight": chains(
            (
                SCALE,
                span(
                    SCALE,
                    NOW - timedelta(days=90),
                    NOW - timedelta(days=40),
                    NOW - timedelta(days=40),
                ),
            )
        ),
    }


def test_renamed_forms_merge_into_one_device_with_the_newest_name():
    devices = build_devices(watch_chains(), now=NOW)
    assert [device["name"] for device in devices] == [
        "Fitbit Air",
        "com.example.health",
        "Aria Example",
    ]
    watch, phone_, scale = devices
    assert watch["previous_names"] == ["Google Fitbit Air"]
    assert watch["data_types"] == ["heart-rate", "steps"] and watch["data_type_count"] == 2
    assert watch["first_seen"] == (NOW - timedelta(days=400)).isoformat()
    assert watch["last_seen"] == NOW.isoformat()
    # The newest record over every data type, not the newest sighting.
    assert watch["last_data"] == (NOW - timedelta(minutes=20)).isoformat()
    assert watch["platform"] == "FITBIT" and watch["recording_method"] == "DERIVED"
    assert len(watch["fingerprints"]) == 2 and all(len(f) == 12 for f in watch["fingerprints"])
    assert [device["status"] for device in devices] == [CONNECTED, DELAYED, DISCONNECTED]
    assert phone_["form_factor"] == "PHONE" and phone_["manufacturer"] == "Example Inc."
    assert phone_["application"] == "com.example.health"  # the per-install suffix is dropped
    assert scale["form_factor"] == "SCALE" and scale["previous_names"] == []
    # Nothing but sanitized source metadata leaves the service.
    assert "data_source" not in watch and "A4B5C6D7E8F9" not in str(devices)


def test_a_rejected_link_keeps_the_two_forms_apart():
    found = watch_chains()
    found["heart-rate"].decide("reject", source_fingerprint(OLD), source_fingerprint(NEW))
    names = [device["name"] for device in build_devices(found, now=NOW)]
    assert names == ["Fitbit Air", "com.example.health", "Google Fitbit Air", "Aria Example"]
    old = next(d for d in build_devices(found, now=NOW) if d["name"] == "Google Fitbit Air")
    assert old["status"] == DISCONNECTED and old["data_types"] == ["heart-rate"]


def test_a_source_without_a_dated_record_is_disconnected_and_last():
    found = {"steps": chains((PHONE, span(PHONE, NOW, NOW, None)), (NEW, span(NEW, NOW, NOW, NOW)))}
    devices = build_devices(found, now=NOW)
    assert [(d["name"], d["status"], d["last_data"]) for d in devices] == [
        ("Fitbit Air", CONNECTED, NOW.isoformat()),
        ("com.example.health", DISCONNECTED, None),
    ]


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (timedelta(0), CONNECTED),
        (timedelta(hours=48), CONNECTED),
        (timedelta(hours=48, seconds=1), DELAYED),
        (timedelta(days=7), DELAYED),
        (timedelta(days=7, seconds=1), DISCONNECTED),
        (None, DISCONNECTED),
    ],
)
def test_default_thresholds(age, expected):
    newest = None if age is None else NOW - age
    assert Thresholds().status(newest, NOW) == expected


def test_thresholds_follow_the_configuration():
    custom = Thresholds(connected_hours=6, delayed_days=1)
    assert custom.status(NOW - timedelta(hours=7), NOW) == DELAYED
    assert custom.status(NOW - timedelta(hours=25), NOW) == DISCONNECTED
    settings = Settings(_env_file=None, devices_connected_hours=6, devices_delayed_days=1)
    assert (settings.devices_connected_hours, settings.devices_delayed_days) == (6, 1)
    assert Settings(_env_file=None).devices_connected_hours == 48
    assert Settings(_env_file=None).devices_delayed_days == 7


def test_google_pairing_matches_by_name_and_keeps_the_rest_apart():
    devices = build_devices(watch_chains(), now=NOW)
    paired = [
        {
            "name": "Fitbit Air",
            "device_type": "WATCH",
            "last_sync_time": "2026-11-07T11:50:00Z",
            "features": [],
        },
        {"name": "Charge 6", "device_type": "TRACKER", "last_sync_time": None, "features": []},
    ]
    unmatched = attach_google(devices, paired)
    assert devices[0]["google_last_sync"] == "2026-11-07T11:50:00Z"
    assert devices[0]["google_device_type"] == "WATCH"
    assert "google_last_sync" not in devices[1] and "google_last_sync" not in devices[2]
    assert [item["name"] for item in unmatched] == ["Charge 6"]
    # A pairing is also recognized through a device's previous name.
    only_old = [
        {
            "name": "Google Fitbit Air",
            "device_type": "WATCH",
            "last_sync_time": "2026-11-07T11:00:00Z",
        }
    ]
    assert attach_google(build_devices(watch_chains(), now=NOW), only_old) == []


async def archive(sessions: async_sessionmaker) -> None:
    """The watch (old and new name, linked), the phone and the scale in SQLite."""
    found = watch_chains()
    async with sessions() as session:
        for data_type, value in found.items():
            await repository.write_spans(session, data_type, value.spans, NOW)
        loaded = await repository.load_chains(session, "heart-rate")
        assert await repository.insert_chain(
            session,
            "heart-rate",
            loaded,
            source_fingerprint(OLD),
            source_fingerprint(NEW),
            evidence_record=ChainEvidence(decided_by="reconciler", now=NOW),
        )
        await session.commit()


async def test_device_report_reads_the_archive_and_google(sessions):
    await archive(sessions)

    async def paired():
        return [
            {"name": "Fitbit Air", "device_type": "WATCH", "last_sync_time": "2026-11-07T11:50:00Z"}
        ]

    async with sessions() as session:
        report = await device_report(session, paired_devices=paired, now=NOW)
    assert report["google"] == "ok" and report["google_unmatched"] == []
    assert report["thresholds"] == {"connected_hours": 48, "delayed_days": 7}
    assert [d["name"] for d in report["devices"]] == [
        "Fitbit Air",
        "com.example.health",
        "Aria Example",
    ]
    assert report["devices"][0]["previous_names"] == ["Google Fitbit Air"]
    assert report["devices"][0]["google_last_sync"] == "2026-11-07T11:50:00Z"
    assert [d["status"] for d in report["devices"]] == [CONNECTED, DELAYED, DISCONNECTED]


@pytest.mark.parametrize("error", [OAuthError("no refresh token"), GoogleHealthError("503")])
async def test_google_failure_leaves_the_archive_devices_in_place(sessions, error):
    await archive(sessions)

    async def paired():
        raise error

    async with sessions() as session:
        report = await device_report(session, paired_devices=paired, now=NOW)
    assert report["google"] == "unavailable"
    assert len(report["devices"]) == 3 and "google_last_sync" not in report["devices"][0]
    async with sessions() as session:
        assert (await device_report(session, now=NOW))["google"] == "not_queried"


async def test_empty_archive_gives_no_devices(sessions):
    async with sessions() as session:
        report = await device_report(session, now=NOW)
    assert report["devices"] == [] and report["google"] == "not_queried"


AUTH = {"Authorization": "Bearer synthetic-devices-test-authentication-token"}


async def test_get_api_devices(sessions, monkeypatch):
    await archive(sessions)
    calls = []

    async def paired():
        calls.append(1)
        if len(calls) > 1:
            raise GoogleHealthError("Google 503")
        return [
            {"name": "Fitbit Air", "device_type": "WATCH", "last_sync_time": "2026-11-07T11:50:00Z"}
        ]

    settings = Settings(_env_file=None, app_env="test", app_ui_token=AUTH["Authorization"][7:])
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(
        main, "build_runtime", lambda *_: SimpleNamespace(discovery=SimpleNamespace(devices=paired))
    )
    app = main.create_app()

    # The report's clock stands still at the archive's NOW: with the wall clock every device
    # read as disconnected 48 hours after that date.
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz)

    monkeypatch.setattr(devices_service, "datetime", Frozen)

    async def session():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://localhost", headers=AUTH
    ) as client:
        denied = await client.get("/api/devices", headers={"Authorization": "Bearer wrong"})
        assert denied.status_code == 401
        response = await client.get("/api/devices")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        payload = response.json()
        assert payload["google"] == "ok"
        assert [d["name"] for d in payload["devices"]] == [
            "Fitbit Air",
            "com.example.health",
            "Aria Example",
        ]
        assert payload["devices"][0]["google_last_sync"] == "2026-11-07T11:50:00Z"
        assert payload["devices"][0]["status"] == CONNECTED
        # Google failing (503 upstream) still answers 200 with the archive's devices.
        again = await client.get("/api/devices")
        assert again.status_code == 200 and again.json()["google"] == "unavailable"
        assert len(again.json()["devices"]) == 3


async def test_umay_devices_action_needs_no_dates_and_reads_the_same_service(sessions, monkeypatch):
    await archive(sessions)
    assert ArchiveQuery(action="devices").start_date is None
    with pytest.raises(ValueError):
        ArchiveQuery(action="daily")
    card = await read_health(sessions, {"action": "devices"})
    assert card["google"] == "not_queried"
    assert [d["name"] for d in card["devices"]] == [
        "Fitbit Air",
        "com.example.health",
        "Aria Example",
    ]
    assert card["devices"][0]["previous_names"] == ["Google Fitbit Air"]
    assert "devices lists" in codex_health.HEALTH_TOOL["description"]
    assert "devices" in codex_health.HEALTH_TOOL["inputSchema"]["properties"]["action"]["enum"]
