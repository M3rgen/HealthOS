"""The user's devices (Cihazlarım): every source the archive has seen, one entry per device.

A device is the set of source fingerprints joined by live chain links (``raw_source_chains``
as ``Chains.members`` reads them), across every data type: "Google Fitbit Air" and "Fitbit
Air" are one watch. The entry carries the newest form's name, the physical time of its newest
record over all its data types (``raw_source_sightings.newest_record_at``) and a freshness
status derived from it. The Garmin source (``garmin_raw_records``, pulled rather than seen
through Google) is one more entry when the archive holds its records. Each Apple Health source
(``apple_sources``: Health Auto Export or an ``export.zip`` import, provider ``APPLE``, source
code ``HEALTH_KIT``) is an entry of its own, or joins the Google entry of the same Apple
device when Google relays it too (platform ``HEALTH_KIT`` and a matching name): one row with
``providers: ["GOOGLE", "APPLE"]``. Every entry says, per category, whether the device is the
primary or a secondary source under the precedence policy
(app/services/source_precedence.py). Read-only; nothing here writes.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.providers import APPLE
from app.models.identity import RawSourceSighting
from app.parsers.identity import stable_package
from app.services.source_chain_evidence import Span
from app.services.source_chain_model import Chains
from app.services.source_chain_repository import load_chains
from app.services.source_precedence import ACTIVITY, GARMIN, GOOGLE, HEALTH, Policy, google_source

DEFAULT_CONNECTED_HOURS = 48
DEFAULT_DELAYED_DAYS = 7
CONNECTED, DELAYED, DISCONNECTED = "bagli", "gecikmeli", "kopuk"
STATUSES = (CONNECTED, DELAYED, DISCONNECTED)
GOOGLE_OK, GOOGLE_UNAVAILABLE, GOOGLE_NOT_QUERIED = "ok", "unavailable", "not_queried"
GARMIN_OK, GARMIN_OFF = "ok", "off"
APPLE_OK, APPLE_OFF = "ok", "off"
HEALTH_KIT = "HEALTH_KIT"


@dataclass(frozen=True)
class Thresholds:
    connected_hours: int = DEFAULT_CONNECTED_HOURS
    delayed_days: int = DEFAULT_DELAYED_DAYS

    def status(self, newest: datetime | None, now: datetime) -> str:
        """``bagli`` while the newest record is at most ``connected_hours`` old, ``gecikmeli``
        up to ``delayed_days``, ``kopuk`` after that (or with no dated record at all)."""
        if newest is None:
            return DISCONNECTED
        age = now - newest
        if age <= timedelta(hours=self.connected_hours):
            return CONNECTED
        if age <= timedelta(days=self.delayed_days):
            return DELAYED
        return DISCONNECTED

    def as_dict(self) -> dict[str, int]:
        return {"connected_hours": self.connected_hours, "delayed_days": self.delayed_days}


DEFAULT_THRESHOLDS = Thresholds()


def _object(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def display_name(span: Span) -> str:
    """What the user calls the source: the device's display name, else its application
    package (without Google's per-install suffix), else the platform, else the report label."""
    source = _object(span.data_source)
    device, application = _object(source.get("device")), _object(source.get("application"))
    package = stable_package(application.get("packageName"))
    return (
        _text(device.get("displayName"))
        or _text(package)
        or _text(source.get("platform"))
        or span.label
    )


def _components(chains_by_type: Mapping[str, Chains]) -> list[frozenset[str]]:
    """Fingerprints joined by live links in any data type form one device (a fingerprint is
    the hash of the dataSource, so one source has the same fingerprint in every data type)."""
    parent: dict[str, str] = {}

    def find(name: str) -> str:
        while parent.setdefault(name, name) != name:
            parent[name] = parent[parent[name]]
            name = parent[name]
        return name

    for chains in chains_by_type.values():
        for fingerprint in chains.spans:
            root = find(fingerprint)
            for member in chains.members(fingerprint):
                other = find(member)
                if other != root:
                    parent[other] = root
    groups: dict[str, set[str]] = {}
    for name in parent:
        groups.setdefault(find(name), set()).add(name)
    return sorted((frozenset(group) for group in groups.values()), key=min)


def _latest_form(
    members: Iterable[str], forms: Mapping[str, list[tuple[str, Span]]], old: set[str]
) -> Span:
    """The newest form of the device: among the forms no live link continues, the one seen
    last (ties: the one with the newest record)."""
    candidates = [name for name in members if name in forms and name not in old]
    if not candidates:
        candidates = [name for name in members if name in forms]
    far_past = datetime.min.replace(tzinfo=UTC)

    def freshness(name: str) -> tuple[datetime, datetime]:
        spans = [span for _, span in forms[name]]
        return (
            max(span.last for span in spans),
            max((span.newest or far_past) for span in spans),
        )

    best = max(candidates, key=freshness)
    return max((span for _, span in forms[best]), key=lambda span: span.last)


def precedence(source_code: str, policy: Policy) -> dict[str, str]:
    """Per category, the device's standing under the policy (primary/secondary/unranked)."""
    return {kind: policy.priority(source_code, kind) for kind in (ACTIVITY, HEALTH)}


def garmin_device(
    summary: Mapping[str, Any],
    *,
    name: str,
    now: datetime,
    policy: Policy,
    thresholds: Thresholds = DEFAULT_THRESHOLDS,
) -> dict[str, Any]:
    """The Cihazlarım entry of the Garmin source from ``garmin_readers.archive_summary``.
    ``last_seen`` is the last successful pull, ``last_data`` the newest normalized record."""
    newest = summary.get("last_data")
    seen = summary.get("last_pull") or summary["first_seen"]
    return {
        "id": "garmin",
        "name": name,
        "platform": GARMIN,
        "provider": GARMIN,
        "source_code": GARMIN,
        "form_factor": "WATCH",
        "manufacturer": "Garmin",
        "application": None,
        "recording_method": None,
        "previous_names": [],
        "fingerprints": [],
        "data_types": list(summary["data_types"]),
        "data_type_count": len(summary["data_types"]),
        "first_seen": summary["first_seen"].isoformat(),
        "last_seen": seen.isoformat(),
        "last_data": newest.isoformat() if newest is not None else None,
        "garmin_last_pull": summary["last_pull"].isoformat() if summary.get("last_pull") else None,
        "status": thresholds.status(newest, now),
        "precedence": precedence(GARMIN, policy),
    }


def _apple_form_factor(*names: object) -> str | None:
    for name in names:
        text = (_text(name) or "").casefold()
        if "watch" in text:
            return "WATCH"
        if "iphone" in text:
            return "PHONE"
        if "ipad" in text:
            return "TABLET"
    return None


def apple_device(
    source: Mapping[str, Any],
    *,
    now: datetime,
    policy: Policy,
    thresholds: Thresholds = DEFAULT_THRESHOLDS,
) -> dict[str, Any]:
    """The Cihazlarım entry of one Apple Health source (``apple_readers.sources`` row).
    ``first_seen``/``last_seen`` are the first and last import that carried it, ``last_data``
    its newest record's time."""
    name = source["source_name"]
    model = _text(source.get("device"))
    newest = source.get("last_data_at")
    apple = model is not None and model.casefold().startswith(("apple", "iphone", "watch", "ipad"))
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:10]
    return {
        "id": f"apple-{digest}",
        "name": name,
        "platform": HEALTH_KIT,
        "provider": APPLE,
        "providers": [APPLE],
        "source_code": HEALTH_KIT,
        "form_factor": _apple_form_factor(model, name),
        "manufacturer": "Apple" if apple else None,
        "model": model,
        "application": None,
        "recording_method": None,
        "previous_names": [],
        "fingerprints": [],
        "data_types": list(source["data_types"]),
        "data_type_count": len(source["data_types"]),
        "first_seen": source["first_seen_at"].isoformat(),
        "last_seen": source["last_seen_at"].isoformat(),
        "last_data": newest.isoformat() if newest is not None else None,
        "apple_last_import": source["last_seen_at"].isoformat(),
        "apple_channels": list(source.get("channels") or []),
        "status": thresholds.status(newest, now),
        "precedence": precedence(HEALTH_KIT, policy),
    }


def attach_apple(
    devices: list[dict[str, Any]],
    sources: Iterable[Mapping[str, Any]],
    *,
    now: datetime,
    policy: Policy,
    thresholds: Thresholds = DEFAULT_THRESHOLDS,
) -> None:
    """Add the Apple sources to ``devices``: an Apple source joins the Google entry of
    platform ``HEALTH_KIT`` whose name matches (``_matches``): ``providers`` gains ``APPLE``,
    ``last_data`` (and the status) follow the newer of the two, ``apple_last_import`` and
    ``apple_data_types`` are added. Any other Apple source is an entry of its own."""
    for source in sources:
        entry = apple_device(source, now=now, policy=policy, thresholds=thresholds)
        match = next(
            (
                device
                for device in devices
                if device.get("provider") == GOOGLE
                and device.get("source_code") == HEALTH_KIT
                and _matches(device, entry)
            ),
            None,
        )
        if match is None:
            devices.append(entry)
            continue
        providers = match.setdefault("providers", [GOOGLE])
        if APPLE not in providers:
            providers.append(APPLE)
        if entry["last_data"] is not None and (
            match["last_data"] is None or entry["last_data"] > match["last_data"]
        ):
            match["last_data"] = entry["last_data"]
            match["status"] = thresholds.status(datetime.fromisoformat(entry["last_data"]), now)
        previous = match.get("apple_last_import")
        if previous is None or entry["apple_last_import"] > previous:
            match["apple_last_import"] = entry["apple_last_import"]
        match["apple_data_types"] = sorted(
            {*match.get("apple_data_types", []), *entry["data_types"]}
        )
        match.setdefault("apple_sources", []).append(entry["name"])
        if match.get("model") is None and entry["model"] is not None:
            match["model"] = entry["model"]


def sort_devices(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Connected first; within a status the freshest device first (UTC ISO strings sort as
    instants), a device without a dated record last; the sorts are stable."""
    order = {status: index for index, status in enumerate(STATUSES)}
    devices.sort(key=lambda item: item["name"].casefold())
    devices.sort(key=lambda item: item["last_data"] or "", reverse=True)
    devices.sort(key=lambda item: order[item["status"]])
    return devices


def build_devices(
    chains_by_type: Mapping[str, Chains],
    *,
    now: datetime,
    thresholds: Thresholds = DEFAULT_THRESHOLDS,
    policy: Policy | None = None,
) -> list[dict[str, Any]]:
    """One entry per device from the loaded chains of every data type (pure; the loading is
    ``load_device_chains``). Sorted by status, then by the newest record, then by name."""
    policy = policy or Policy.from_settings()
    forms: dict[str, list[tuple[str, Span]]] = {}
    old: set[str] = set()
    for data_type, chains in chains_by_type.items():
        for fingerprint, span in chains.spans.items():
            forms.setdefault(fingerprint, []).append((data_type, span))
        old.update(chains.following)
    devices = []
    for members in _components(chains_by_type):
        spans = [span for name in members for _, span in forms.get(name, ())]
        if not spans:
            continue
        latest = _latest_form(members, forms, old)
        source = _object(latest.data_source)
        device, application = _object(source.get("device")), _object(source.get("application"))
        name = display_name(latest)
        newest = max((span.newest for span in spans if span.newest is not None), default=None)
        data_types = sorted({data_type for member in members for data_type, _ in forms[member]})
        previous = sorted(
            {
                display_name(span)
                for member in members
                for _, span in forms[member]
                if display_name(span) != name
            }
        )
        devices.append(
            {
                "id": min(members)[:12],
                "name": name,
                "platform": _text(source.get("platform")),
                "provider": GOOGLE,
                "source_code": google_source(source.get("platform")),
                "form_factor": _text(device.get("formFactor")),
                "manufacturer": _text(device.get("manufacturer")),
                "application": _text(stable_package(application.get("packageName"))),
                "recording_method": _text(source.get("recordingMethod")),
                "previous_names": previous,
                "fingerprints": sorted(member[:12] for member in members),
                "data_types": data_types,
                "data_type_count": len(data_types),
                "first_seen": min(span.first for span in spans).isoformat(),
                "last_seen": max(span.last for span in spans).isoformat(),
                "last_data": newest.isoformat() if newest is not None else None,
                "status": thresholds.status(newest, now),
                "precedence": precedence(google_source(source.get("platform")), policy),
            }
        )
    return sort_devices(devices)


async def load_device_chains(session: AsyncSession) -> dict[str, Chains]:
    """The chains (links, decisions and sighting spans) of every data type that has a
    sighting; the input of ``build_devices``."""
    data_types = (
        await session.scalars(
            select(RawSourceSighting.data_type).distinct().order_by(RawSourceSighting.data_type)
        )
    ).all()
    return {data_type: await load_chains(session, data_type) for data_type in data_types}


def _normalized(name: object) -> str:
    text = " ".join((_text(name) or "").casefold().split())
    return text.removeprefix("google ")


def _matches(device: Mapping[str, Any], paired: Mapping[str, Any]) -> bool:
    """A paired Google device is this archive device when their names agree after dropping
    brand prefixes, or one name contains the other ("Fitbit Air" and "Google Fitbit Air")."""
    names = {_normalized(device["name"]), *(_normalized(name) for name in device["previous_names"])}
    other = _normalized(paired.get("name"))
    if not other:
        return False
    return any(name and (name == other or name in other or other in name) for name in names)


def attach_google(
    devices: list[dict[str, Any]], paired: Iterable[Mapping[str, Any]] | None
) -> list[dict[str, Any]]:
    """Add ``google_last_sync`` (and the paired device type) to the archive devices Google
    lists as paired; the paired devices no archive device matches are returned apart."""
    unmatched: list[dict[str, Any]] = []
    for item in paired or ():
        found = [device for device in devices if _matches(device, item)]
        if not found:
            unmatched.append(dict(item))
            continue
        for device in found:
            device["google_last_sync"] = item.get("last_sync_time")
            device["google_device_type"] = item.get("device_type")
    return unmatched


async def device_report(
    session: AsyncSession,
    *,
    paired_devices: Callable[[], Any] | None = None,
    thresholds: Thresholds = DEFAULT_THRESHOLDS,
    now: datetime | None = None,
    policy: Policy | None = None,
    garmin_name: str = "Garmin",
) -> dict[str, Any]:
    """The Cihazlarım card: the archive's devices, with Google's paired-device sync times when
    ``paired_devices`` (an awaitable factory, ``DiscoveryService.devices``) answers. A Google
    failure never hides the archive: ``google`` is ``unavailable`` and the devices still come.
    The Garmin source is listed (``garmin: ok``) when the archive holds Garmin records, the
    Apple Health sources (``apple: ok``) when ``apple_sources`` has any.
    """
    from app.google_health.client import GoogleHealthError
    from app.google_health.oauth import OAuthError
    from app.services.apple_readers import sources as apple_sources
    from app.services.garmin_readers import archive_summary

    now = now or datetime.now(UTC)
    policy = policy or Policy.from_settings()
    devices = build_devices(
        await load_device_chains(session), now=now, thresholds=thresholds, policy=policy
    )
    garmin, summary = GARMIN_OFF, await archive_summary(session)
    if summary is not None:
        garmin = GARMIN_OK
        devices.append(
            garmin_device(summary, name=garmin_name, now=now, policy=policy, thresholds=thresholds)
        )
        sort_devices(devices)
    apple, found = APPLE_OFF, await apple_sources(session)
    if found:
        apple = APPLE_OK
        attach_apple(devices, found, now=now, policy=policy, thresholds=thresholds)
        sort_devices(devices)
    google, paired = GOOGLE_NOT_QUERIED, None
    if paired_devices is not None:
        try:
            paired = await paired_devices()
            google = GOOGLE_OK
        except (OAuthError, GoogleHealthError):
            google = GOOGLE_UNAVAILABLE
    unmatched = attach_google(devices, paired)
    return {
        "checked_at": now.isoformat(),
        "thresholds": thresholds.as_dict(),
        "google": google,
        "garmin": garmin,
        "apple": apple,
        "precedence": policy.as_dict(),
        "devices": devices,
        "google_unmatched": unmatched,
        "note": (
            "Son veri, kaynağın arşivdeki en yeni kaydının fiziksel zamanı; eşitleme zamanı "
            "değil. Aynı cihazın eski adları tek satırda birleşir. Öncelik, aynı veriyi birden "
            "fazla cihaz verdiğinde hangisinin önde gösterildiğini söyler; diğerleri görünür "
            "kalır, toplanmaz."
        ),
    }
