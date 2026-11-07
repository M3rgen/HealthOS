"""Synthetic Apple Health data for tests: export.xml documents, export.zip archives (also
streamed, never materialized) and Health Auto Export request bodies.

Every value is made up (sources "Test Watch"/"Test iPhone"); the shapes follow Apple's
export.xml and Health Auto Export's documented format (see app/apple/export_xml.py and
app/apple/hae.py).
"""

from __future__ import annotations

import io
import json
import struct
import zipfile
import zlib
from collections.abc import Iterable, Iterator, Mapping
from xml.sax.saxutils import quoteattr

WATCH = "Test Watch"
PHONE = "Test iPhone"
WATCH_DEVICE = (
    "<<HKDevice: 0x600000001>, name:Apple Watch, manufacturer:Apple Inc., model:Watch, "
    "hardware:Watch6,1, software:10.1>"
)
PHONE_DEVICE = (
    "<<HKDevice: 0x600000002>, name:iPhone, manufacturer:Apple Inc., model:iPhone, "
    "hardware:iPhone15,2, software:17.1>"
)
DOCTYPE = """<!DOCTYPE HealthData [
<!-- HealthKit Export Version: 14 -->
<!ELEMENT HealthData (ExportDate,Me,(Record|Correlation|Workout|ActivitySummary|ClinicalRecord|
  Audiogram|VisionPrescription)*)>
<!ATTLIST HealthData
  locale CDATA #REQUIRED
>
<!ELEMENT ExportDate EMPTY>
<!ATTLIST ExportDate
  value CDATA #REQUIRED
>
<!ELEMENT Record ((MetadataEntry|HeartRateVariabilityMetadataList)*)>
<!ATTLIST Record
  type          CDATA #REQUIRED
  unit          CDATA #IMPLIED
  value         CDATA #IMPLIED
  sourceName    CDATA #REQUIRED
  sourceVersion CDATA #IMPLIED
  device        CDATA #IMPLIED
  creationDate  CDATA #IMPLIED
  startDate     CDATA #REQUIRED
  endDate       CDATA #REQUIRED
>
<!ELEMENT MetadataEntry EMPTY>
<!ATTLIST MetadataEntry
  key   CDATA #REQUIRED
  value CDATA #REQUIRED
>
]>
"""


def element(tag: str, attrs: Mapping[str, object], *children: str) -> str:
    """One XML element with its attributes (escaped) and nested element texts."""
    text = "".join(f" {name}={quoteattr(str(value))}" for name, value in attrs.items())
    if not children:
        return f"<{tag}{text}/>"
    return f"<{tag}{text}>\n" + "\n".join(f"  {child}" for child in children) + f"\n</{tag}>"


def metadata(**entries: object) -> list[str]:
    return [
        element("MetadataEntry", {"key": key, "value": value}) for key, value in entries.items()
    ]


def record(
    record_type: str = "HKQuantityTypeIdentifierStepCount",
    value: object = "120",
    *,
    start: str = "2026-11-06 10:00:00 +0100",
    end: str = "2026-11-06 10:05:00 +0100",
    source: str = WATCH,
    device: str | None = WATCH_DEVICE,
    unit: str | None = "count",
    children: Iterable[str] = (),
    **extra: object,
) -> str:
    attrs: dict[str, object] = {"type": record_type, "sourceName": source, "sourceVersion": "10.1"}
    if device is not None:
        attrs["device"] = device
    if unit is not None:
        attrs["unit"] = unit
    attrs.update(creationDate=end, startDate=start, endDate=end)
    if value is not None:
        attrs["value"] = value
    attrs.update(extra)
    return element("Record", attrs, *children)


def hrv_record(beats: Iterable[tuple[int, str]] = ((61, "10:01:02.11 PM"), (63, "10:01:03.05 PM"))):
    items = [element("InstantaneousBeatsPerMinute", {"bpm": bpm, "time": t}) for bpm, t in beats]
    return record(
        "HKQuantityTypeIdentifierHeartRateVariabilitySDNN",
        "48.5",
        unit="ms",
        children=[element("HeartRateVariabilityMetadataList", {}, *items)],
    )


def sleep_record(stage: str = "AsleepCore", **kwargs) -> str:
    return record(
        "HKCategoryTypeIdentifierSleepAnalysis",
        f"HKCategoryValueSleepAnalysis{stage}",
        unit=None,
        **kwargs,
    )


def blood_pressure(systolic: int = 120, diastolic: int = 80, at: str = "2026-11-06 08:00:00 +0100"):
    common = {"sourceName": PHONE, "creationDate": at, "startDate": at, "endDate": at}
    return element(
        "Correlation",
        {"type": "HKCorrelationTypeIdentifierBloodPressure", **common},
        *metadata(HKWasUserEntered="1"),
        record(
            "HKQuantityTypeIdentifierBloodPressureSystolic",
            systolic,
            start=at,
            end=at,
            source=PHONE,
            device=None,
            unit="mmHg",
        ),
        record(
            "HKQuantityTypeIdentifierBloodPressureDiastolic",
            diastolic,
            start=at,
            end=at,
            source=PHONE,
            device=None,
            unit="mmHg",
        ),
    )


def workout(
    activity: str = "HKWorkoutActivityTypeRunning",
    *,
    start: str = "2026-11-06 17:00:00 +0100",
    end: str = "2026-11-06 17:30:30 +0100",
    route: str | None = "/workout-routes/route_2026-11-06_5.00pm.gpx",
    source: str = WATCH,
    totals: bool = True,
) -> str:
    attrs: dict[str, object] = {
        "workoutActivityType": activity,
        "duration": "30.5",
        "durationUnit": "min",
    }
    if totals:
        attrs.update(
            totalDistance="5.1",
            totalDistanceUnit="km",
            totalEnergyBurned="300",
            totalEnergyBurnedUnit="kcal",
        )
    attrs.update(
        sourceName=source,
        sourceVersion="10.1",
        device=WATCH_DEVICE,
        creationDate=end,
        startDate=start,
        endDate=end,
    )
    children = [
        *metadata(HKIndoorWorkout="0", HKAverageMETs="9.5 kcal/hr·kg"),
        element(
            "WorkoutEvent",
            {
                "type": "HKWorkoutEventTypeSegment",
                "date": start,
                "duration": "10",
                "durationUnit": "min",
            },
        ),
        element("WorkoutEvent", {"type": "HKWorkoutEventTypePause", "date": start}),
        element(
            "WorkoutStatistics",
            {
                "type": "HKQuantityTypeIdentifierActiveEnergyBurned",
                "startDate": start,
                "endDate": end,
                "sum": "250",
                "unit": "kcal",
            },
        ),
        element(
            "WorkoutStatistics",
            {
                "type": "HKQuantityTypeIdentifierHeartRate",
                "startDate": start,
                "endDate": end,
                "average": "150",
                "minimum": "120",
                "maximum": "175",
                "unit": "count/min",
            },
        ),
    ]
    if route is not None:
        children.append(
            element(
                "WorkoutRoute",
                {"sourceName": source, "creationDate": end, "startDate": start, "endDate": end},
                *metadata(HKMetadataKeySyncVersion="2"),
                element("FileReference", {"path": route}),
            )
        )
    return element("Workout", attrs, *children)


def activity_summary(day: str = "2026-11-06", energy: str = "450.5") -> str:
    return element(
        "ActivitySummary",
        {
            "dateComponents": day,
            "activeEnergyBurned": energy,
            "activeEnergyBurnedGoal": "500",
            "activeEnergyBurnedUnit": "kcal",
            "appleMoveTime": "0",
            "appleMoveTimeGoal": "0",
            "appleExerciseTime": "35",
            "appleExerciseTimeGoal": "30",
            "appleStandHours": "11",
            "appleStandHoursGoal": "12",
        },
    )


def export_head(
    *, locale: str = "en_US", export_date: str = "2026-11-07 10:00:00 +0100", doctype: bool = True
) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        + (DOCTYPE if doctype else "")
        + f'<HealthData locale="{locale}">\n'
        + f' <ExportDate value="{export_date}"/>\n'
        + ' <Me HKCharacteristicTypeIdentifierDateOfBirth="" '
        'HKCharacteristicTypeIdentifierBiologicalSex="HKBiologicalSexNotSet"/>\n'
    )


EXPORT_TAIL = "</HealthData>\n"


def export_xml(*elements: str, **head) -> bytes:
    """A whole export.xml holding the given elements."""
    return (export_head(**head) + "".join(f" {item}\n" for item in elements) + EXPORT_TAIL).encode()


def full_export() -> bytes:
    """One of every element kind (and elements the parser skips)."""
    return export_xml(
        record(),
        record(
            "HKQuantityTypeIdentifierHeartRate",
            "62",
            unit="count/min",
            children=metadata(HKMetadataKeyHeartRateMotionContext="0"),
        ),
        hrv_record(),
        sleep_record(
            "AsleepCore", start="2026-11-06 01:00:00 +0100", end="2026-11-06 02:00:00 +0100"
        ),
        record("HKDataTypeSleepDurationGoal", "8", unit="hr"),
        blood_pressure(),
        workout(),
        activity_summary(),
        element(
            "ClinicalRecord",
            {
                "type": "HKClinicalTypeIdentifierLabResultRecord",
                "identifier": "x",
                "sourceName": "Test Clinic",
            },
        ),
        element(
            "Audiogram",
            {"type": "HKDataTypeIdentifierAudiogram", "sourceName": PHONE},
            element("SensitivityPoint", {"frequencyValue": "1000"}),
        ),
    )


# -- zip archives ---------------------------------------------------------------------------


def zip_bytes(
    entries: Iterable[tuple[str, bytes]],
    *,
    compression: int = zipfile.ZIP_DEFLATED,
    streaming: bool = False,
) -> bytes:
    """An in-memory zip; ``streaming`` writes it to a non-seekable stream, so that every
    entry carries a data descriptor (sizes after the data)."""
    target = io.BytesIO()
    stream = _Unseekable(target) if streaming else target
    with zipfile.ZipFile(stream, "w", compression=compression) as archive:  # type: ignore[arg-type]
        for name, data in entries:
            if streaming:
                with archive.open(name, "w") as member:
                    member.write(data)
            else:
                archive.writestr(name, data)
    return target.getvalue()


class _Unseekable:
    def __init__(self, target: io.BytesIO) -> None:
        self._target = target

    def write(self, data: bytes) -> int:
        return self._target.write(data)

    def flush(self) -> None:
        pass


def export_zip(xml: bytes, *, name: str = "apple_health_export/export.xml", **options) -> bytes:
    """A Health-app-like export.zip: the export, its clinical twin and a workout route."""
    return zip_bytes(
        [
            (name, xml),
            ("apple_health_export/export_cda.xml", b'<?xml version="1.0"?><ClinicalDocument/>'),
            (
                "apple_health_export/workout-routes/route_2026-11-06_5.00pm.gpx",
                b'<?xml version="1.0"?><gpx version="1.1"><trk/></gpx>',
            ),
        ],
        **options,
    )


def streamed_zip(name: str, chunks: Iterable[bytes], level: int = 1) -> Iterator[bytes]:
    """A deflated single-entry zip produced chunk by chunk from ``chunks`` (never held in
    memory): local header with the data-descriptor flag, deflate stream, zip64 data
    descriptor, central directory."""
    raw_name = name.encode()
    # Version 4.5 (zip64), flag 0x08 (sizes in the data descriptor), deflate.
    extra = struct.pack("<HHQQ", 0x0001, 16, 0, 0)
    header = struct.pack(
        "<4sHHHHHIIIHH",
        b"PK\x03\x04",
        45,
        0x08,
        8,
        0,
        0x21,
        0,
        0xFFFFFFFF,
        0xFFFFFFFF,
        len(raw_name),
        len(extra),
    )
    yield header + raw_name + extra
    compressor = zlib.compressobj(level, zlib.DEFLATED, -15)
    crc = size = compressed = 0
    for chunk in chunks:
        crc = zlib.crc32(chunk, crc)
        size += len(chunk)
        out = compressor.compress(chunk)
        compressed += len(out)
        if out:
            yield out
    out = compressor.flush()
    compressed += len(out)
    yield out
    yield struct.pack("<4sIQQ", b"PK\x07\x08", crc, compressed, size)
    offset = len(header) + len(raw_name) + len(extra) + compressed + 24
    central_extra = struct.pack("<HHQQQ", 0x0001, 24, size, compressed, 0)
    central = struct.pack(
        "<4sHHHHHHIIIHHHHHII",
        b"PK\x01\x02",
        45,
        45,
        0x08,
        8,
        0,
        0x21,
        crc,
        0xFFFFFFFF,
        0xFFFFFFFF,
        len(raw_name),
        len(central_extra),
        0,
        0,
        0,
        0,
        0xFFFFFFFF,
    )
    directory = central + raw_name + central_extra
    yield directory
    yield struct.pack(
        "<4sHHHHIIH", b"PK\x05\x06", 0, 0, 1, 1, len(directory), min(offset, 0xFFFFFFFF), 0
    )


def generated_export(records: int, *, batch: int = 500) -> Iterator[bytes]:
    """An export.xml of ``records`` step records, generated in chunks (never whole)."""
    yield export_head().encode()
    template = (
        ' <Record type="HKQuantityTypeIdentifierStepCount" sourceName="Test Watch" '
        'sourceVersion="10.1" device="&lt;&lt;HKDevice: 0x600000001&gt;, name:Apple Watch, '
        'manufacturer:Apple Inc., model:Watch, hardware:Watch6,1, software:10.1&gt;" '
        'unit="count" creationDate="2026-11-06 10:{m:02d}:00 +0100" '
        'startDate="2026-{mo:02d}-{d:02d} {h:02d}:{m:02d}:00 +0100" '
        'endDate="2026-{mo:02d}-{d:02d} {h:02d}:{m:02d}:59 +0100" value="{v}"/>\n'
    )
    for first in range(0, records, batch):
        yield "".join(
            template.format(
                mo=1 + n // 40000 % 12, d=1 + n // 1440 % 28, h=n // 60 % 24, m=n % 60, v=n
            )
            for n in range(first, min(first + batch, records))
        ).encode()
    yield EXPORT_TAIL.encode()


def chunked(data: bytes, size: int) -> Iterator[bytes]:
    for offset in range(0, len(data), size):
        yield data[offset : offset + size]


# -- Health Auto Export -----------------------------------------------------------------------


def hae_body(*, metrics: list | None = None, workouts: list | None = None, **arrays) -> bytes:
    data: dict[str, object] = {}
    if metrics is not None:
        data["metrics"] = metrics
    if workouts is not None:
        data["workouts"] = workouts
    data.update(arrays)
    return json.dumps({"data": data}).encode()


def hae_metric(name: str, units: str, *points: dict) -> dict:
    return {"name": name, "units": units, "data": list(points)}


def hae_steps(qty: float = 1234, date: str = "2026-11-06 00:00:00 +0100") -> dict:
    return hae_metric("step_count", "count", {"date": date, "qty": qty, "source": WATCH})


def hae_heart_rate(date: str = "2026-11-06 10:00:00 +0100") -> dict:
    return hae_metric(
        "heart_rate",
        "count/min",
        {"date": date, "Min": 55, "Avg": 68.5, "Max": 120, "source": WATCH},
    )


def hae_sleep_summary(date: str = "2026-11-06 00:00:00 +0100") -> dict:
    return hae_metric(
        "sleep_analysis",
        "hr",
        {
            "date": date,
            "totalSleep": 7.25,
            "asleep": 0,
            "core": 4.0,
            "deep": 1.25,
            "rem": 2.0,
            "awake": 0.5,
            "sleepStart": "2026-11-05 23:10:00 +0100",
            "sleepEnd": "2026-11-06 06:55:00 +0100",
            "inBed": 7.75,
            "inBedStart": "2026-11-05 23:00:00 +0100",
            "inBedEnd": "2026-11-06 07:00:00 +0100",
            "source": WATCH,
        },
    )


def hae_sleep_stages() -> dict:
    def stage(value: str, start: str, end: str, qty: float) -> dict:
        return {
            "date": start,
            "start": start,
            "end": end,
            "startDate": start,
            "endDate": end,
            "qty": qty,
            "value": value,
            "source": WATCH,
        }

    return hae_metric(
        "sleep_analysis",
        "hr",
        stage("Core", "2026-11-05 23:10:00 +0100", "2026-11-06 01:10:00 +0100", 2.0),
        stage("Deep", "2026-11-06 01:10:00 +0100", "2026-11-06 02:25:00 +0100", 1.25),
        stage("REM", "2026-11-06 02:25:00 +0100", "2026-11-06 04:25:00 +0100", 2.0),
    )


def hae_blood_pressure(date: str = "2026-11-06 08:00:00 +0100") -> dict:
    return hae_metric(
        "blood_pressure", "mmHg", {"date": date, "systolic": 120, "diastolic": 80, "source": PHONE}
    )


def hae_workout_v2(workout_id: str = "11111111-2222-3333-4444-555555555555") -> dict:
    return {
        "id": workout_id,
        "name": "Outdoor Run",
        "start": "2026-11-06 17:00:00 +0100",
        "end": "2026-11-06 17:30:30 +0100",
        "duration": 1830,
        "source": {"name": WATCH, "identifier": "com.apple.health.synthetic"},
        "location": "Outdoor",
        "isIndoor": False,
        "activeEnergyBurned": {"qty": 300, "units": "kcal"},
        "distance": {"qty": 5.1, "units": "km"},
        "heartRate": {"min": {"qty": 120}, "avg": {"qty": 150}, "max": {"qty": 175}},
        "heartRateData": [
            {"date": "2026-11-06 17:01:00 +0100", "Min": 120, "Avg": 130, "Max": 140}
        ],
        "route": [],
    }


def hae_workout_v1() -> dict:
    return {
        "name": "Walking",
        "start": "2026-11-06 12:00:00 +0100",
        "end": "2026-11-06 12:40:00 +0100",
        "activeEnergy": {"qty": 150, "units": "kcal"},
        "totalEnergy": {"qty": 190, "units": "kcal"},
        "heartRateData": [{"date": "2026-11-06 12:01:00 +0100", "qty": 98, "units": "count/min"}],
    }
