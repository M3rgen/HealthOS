"""Project activity FIT messages into the shared archive identity contract.

The full binary (including unknown/native/developer fields) is kept losslessly as
base64 in a namespaced raw file record. Projection payloads contain only interpreted
fields, so file metadata, definition order and developer bytes do not change identity
or readings. No Google resource name, fetch receipt or deletion evidence is invented.
"""

import base64
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from app.core.fit import FIT_EXERCISE, FIT_FILE, FIT_HEART_RATE, MAX_FIT_RECORDS
from app.core.records import RecordType
from app.parsers.common import payload_hash

from .fit_decode import FitError, messages

EPOCH = datetime(1989, 12, 31, tzinfo=UTC)
SPORTS = {1: "RUNNING", 2: "CYCLING", 5: "SWIMMING", 11: "WALKING", 17: "HIKING"}


@dataclass(frozen=True)
class FitImport:
    sha256: str
    file: dict
    records: dict[RecordType, list[dict]]


def _instant(value: int | None) -> datetime | None:
    if value is None:
        return None
    if not 0 <= value < 0xFFFFFFFF:
        raise FitError("FIT zaman damgası geçersiz.")
    return EPOCH + timedelta(seconds=value)


def _source(file_id: dict[int, int | None], digest: str) -> dict:
    if file_id.get(0) != 4 or file_id.get(1) is None:
        raise FitError("FIT etkinlik dosyası gerekli.")
    # Serial distinguishes physical devices; unknown devices are kept conservatively
    # apart by creation time (or the file hash when no creation time was provided).
    serial = file_id.get(3)
    device: dict[str, int | str | None] = {
        "manufacturer": file_id[1],
        "product": file_id.get(2),
        "serial": serial,
    }
    if serial is None:
        device["creation"] = file_id.get(4) if file_id.get(4) is not None else digest
    if file_id[1] != 1:
        return {
            "platform": "FIT",
            "recordingMethod": "AUTOMATIC",
            "application": {"packageName": "fit." + payload_hash(device)},
            "device": {
                "manufacturer": f"FIT manufacturer {file_id[1]}",
                "displayName": "FIT dosyası",
            },
        }
    return {
        "platform": "GARMIN",
        "recordingMethod": "AUTOMATIC",
        "application": {"packageName": "garmin.fit." + payload_hash(device)},
        "device": {"manufacturer": "Garmin", "displayName": "Garmin FIT"},
    }


def _session(fields: dict[int, int | None]) -> dict:
    start, end = _instant(fields.get(2)), _instant(fields.get(253))
    elapsed = fields.get(7)
    timer = fields.get(8)
    if any(value is not None and not 0 <= value < 0xFFFFFFFF for value in (elapsed, timer)):
        raise FitError("FIT etkinlik süresi geçersiz.")
    if end is None and start is not None and elapsed is not None:
        end = start + timedelta(milliseconds=elapsed)
    if start is None or end is None or end <= start:
        raise FitError("FIT etkinlik zaman aralığı geçersiz.")
    duration = timer if timer is not None else elapsed
    sport = fields.get(5)
    metrics = {}
    if (distance := fields.get(9)) is not None:
        metrics["distanceMillimeters"] = distance * 10  # FIT centimetres -> millimetres
    if (calories := fields.get(11)) is not None:
        metrics["caloriesKcal"] = calories
    return {
        "interval": {"startTime": start.isoformat(), "endTime": end.isoformat()},
        "exerciseType": SPORTS.get(sport, f"FIT_SPORT_{sport}") if sport is not None else "OTHER",
        "activeDuration": duration / 1000 if duration is not None else None,
        "metricsSummary": metrics,
        "fitSport": sport,
        "fitSubSport": fields.get(6),
    }


def parse_fit(data: bytes) -> FitImport:
    """Fully validate first; a damaged tail cannot commit an earlier activity."""
    digest = sha256(data).hexdigest()
    file_id = None
    sessions, hearts = [], []
    count = 0
    for kind, fields in messages(data):
        if kind == 0:
            if file_id is not None:
                raise FitError("FIT dosyasında birden fazla dosya kimliği var.")
            file_id = fields
        elif kind == 18:
            sessions.append(_session(fields))
        elif kind == 20 and fields.get(3) is not None:
            moment = _instant(fields.get(253))
            if moment is None:
                raise FitError("FIT nabız örneğinin zaman damgası eksik.")
            hearts.append(
                {"sampleTime": {"physicalTime": moment.isoformat()}, "beatsPerMinute": fields[3]}
            )
        count = len(sessions) + len(hearts)
        if count > MAX_FIT_RECORDS:
            raise FitError("FIT kayıt sınırı aşıldı.")
    if file_id is None or not count:
        raise FitError("FIT dosyasında içe aktarılabilir etkinlik yok.")
    source = _source(file_id, digest)
    records = {
        spec: [{"dataSource": source, spec.json_key: payload} for payload in payloads]
        for spec, payloads in ((FIT_EXERCISE, sessions), (FIT_HEART_RATE, hearts))
        if payloads
    }
    file = {
        "name": f"garmin-fit:file:{digest}",
        FIT_FILE.json_key: {
            "sha256": digest,
            "encoding": "base64",
            "body": base64.b64encode(data).decode("ascii"),
            "parserVersion": 1,
        },
    }
    return FitImport(digest, file, records)
