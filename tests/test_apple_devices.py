"""Cihazlarım with Apple Health sources (app/services/devices.py).

Each ``apple_sources`` row is an entry (provider ``APPLE``, source code ``HEALTH_KIT``), or
joins the Google entry of the same Apple device (platform ``HEALTH_KIT``, matching name);
``apple`` says whether any Apple source exists. Synthetic sources only.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from identity_records import STEPS, fitbit, steps, store

from app.core.providers import APPLE, GOOGLE
from app.models.apple import AppleSource
from app.services.dashboard_views.devices import (
    CONNECTED,
    DELAYED,
    DISCONNECTED,
    apple_device,
    attach_apple,
    device_report,
)
from app.services.identities.source_precedence import Policy

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
WATCH_SOURCE = {
    "platform": "HEALTH_KIT",
    "recordingMethod": "PASSIVELY_MEASURED",
    "device": {"displayName": "Test Watch", "formFactor": "WATCH", "manufacturer": "Apple"},
}


def source(name: str, device: str | None, last_data: datetime | None, **extra) -> AppleSource:
    return AppleSource(
        source_name=name,
        device=device,
        first_seen_at=NOW - timedelta(days=10),
        last_seen_at=NOW - timedelta(hours=1),
        last_data_at=last_data,
        data_types=extra.get("data_types", ["HKQuantityTypeIdentifierStepCount"]),
        channels=extra.get("channels", ["export"]),
        records=extra.get("records", 5),
    )


async def add(sessions, *rows: AppleSource) -> None:
    async with sessions.begin() as session:
        session.add_all(rows)


async def test_no_apple_source_means_apple_off_and_nothing_else_changes(sessions, clock):
    clock.now = NOW
    await store(sessions, STEPS, steps(10, NOW - timedelta(hours=5), source=fitbit("Fitbit Air")))
    async with sessions() as session:
        report = await device_report(session, now=NOW)
    assert report["apple"] == "off"
    (device,) = report["devices"]
    assert device["provider"] == GOOGLE and "providers" not in device
    assert "apple_last_import" not in device


async def test_each_apple_source_is_an_entry_of_its_own(sessions):
    await add(
        sessions,
        source("Test Watch", "Apple Watch", NOW - timedelta(hours=3)),
        source("Test iPhone", "iPhone", NOW - timedelta(days=3), channels=["export", "hae"]),
        source("Sleep App", None, NOW - timedelta(days=30)),
    )
    async with sessions() as session:
        report = await device_report(session, now=NOW)
    assert report["apple"] == "ok"
    by_name = {device["name"]: device for device in report["devices"]}
    watch, iphone, app = by_name["Test Watch"], by_name["Test iPhone"], by_name["Sleep App"]
    assert watch["id"].startswith("apple-") and len(watch["id"]) == len("apple-") + 10
    assert watch["id"] != iphone["id"]
    assert (watch["provider"], watch["source_code"], watch["platform"]) == (
        APPLE,
        "HEALTH_KIT",
        "HEALTH_KIT",
    )
    assert watch["providers"] == [APPLE]
    assert (watch["manufacturer"], watch["form_factor"]) == ("Apple", "WATCH")
    assert (iphone["manufacturer"], iphone["form_factor"]) == ("Apple", "PHONE")
    assert (app["manufacturer"], app["form_factor"]) == (None, None)
    assert (watch["status"], iphone["status"], app["status"]) == (
        CONNECTED,
        DELAYED,
        DISCONNECTED,
    )
    assert watch["data_types"] == ["HKQuantityTypeIdentifierStepCount"]
    assert watch["first_seen"] == (NOW - timedelta(days=10)).isoformat()
    assert (
        watch["apple_last_import"] == watch["last_seen"] == (NOW - timedelta(hours=1)).isoformat()
    )
    assert iphone["apple_channels"] == ["export", "hae"]
    assert watch["precedence"] == {"activity": "secondary", "health": "secondary"}
    # Sorted like every entry: connected first, then by the newest record.
    assert [device["name"] for device in report["devices"]] == [
        "Test Watch",
        "Test iPhone",
        "Sleep App",
    ]


async def test_an_apple_source_joins_the_google_health_kit_entry_of_the_same_name(sessions, clock):
    clock.now = NOW
    google_data = NOW - timedelta(days=4)
    await store(sessions, STEPS, steps(10, google_data, source=WATCH_SOURCE))
    await add(sessions, source("Test Watch", "Apple Watch", NOW - timedelta(hours=2)))
    async with sessions() as session:
        report = await device_report(session, now=NOW)
    (device,) = report["devices"]
    assert device["provider"] == GOOGLE and device["providers"] == [GOOGLE, APPLE]
    assert device["last_data"] == (NOW - timedelta(hours=2)).isoformat()  # the newer one
    assert device["status"] == CONNECTED
    assert device["apple_last_import"] == (NOW - timedelta(hours=1)).isoformat()
    assert device["apple_data_types"] == ["HKQuantityTypeIdentifierStepCount"]
    assert device["data_types"] == ["steps"]  # Google's own list stays as it was


async def test_different_names_stay_apart(sessions, clock):
    clock.now = NOW
    await store(sessions, STEPS, steps(10, NOW - timedelta(hours=5), source=WATCH_SOURCE))
    await add(sessions, source("Test iPhone", "iPhone", NOW - timedelta(hours=2)))
    async with sessions() as session:
        report = await device_report(session, now=NOW)
    by_name = {device["name"]: device for device in report["devices"]}
    assert set(by_name) == {"Test Watch", "Test iPhone"}
    assert "providers" not in by_name["Test Watch"]
    assert by_name["Test iPhone"]["provider"] == APPLE


def test_an_apple_source_never_joins_a_fitbit_entry_of_the_same_name():
    google = {
        "name": "Test Watch",
        "previous_names": [],
        "provider": GOOGLE,
        "source_code": "FITBIT",
        "last_data": None,
    }
    devices = [google]
    entry = {
        "source_name": "Test Watch",
        "device": "Apple Watch",
        "first_seen_at": NOW,
        "last_seen_at": NOW,
        "last_data_at": NOW,
        "data_types": [],
        "channels": ["hae"],
    }
    attach_apple(devices, [entry], now=NOW, policy=Policy())
    assert len(devices) == 2 and "providers" not in google
    assert devices[1] == apple_device(entry, now=NOW, policy=Policy())
