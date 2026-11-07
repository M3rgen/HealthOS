"""The streaming export.xml parser: record shapes, chunk independence and refusals.

Synthetic documents only (tests/apple_fixtures.py).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from apple_fixtures import (
    PHONE,
    WATCH,
    activity_summary,
    blood_pressure,
    chunked,
    element,
    export_xml,
    full_export,
    metadata,
    record,
    workout,
)

from app.apple.export_xml import ExportFormatError, ExportParser
from app.apple.records import (
    ACTIVITY_SUMMARY,
    CATEGORY,
    EXPORT,
    QUANTITY,
    RECORD,
    WORKOUT,
    body_digest,
    identity_key,
)


def parse(data: bytes, size: int | None = None):
    parser = ExportParser()
    records = []
    for chunk in chunked(data, size or max(len(data), 1)):
        records += parser.feed(chunk)
    records += parser.close()
    return parser, records


def by_type(records):
    return {item.type: item for item in records}


def test_every_element_kind_becomes_its_record():
    parser, records = parse(full_export())
    assert parser.export_date == datetime(2026, 11, 7, 9, 0, tzinfo=UTC)
    assert parser.locale == "en_US"
    assert parser.counts == {QUANTITY: 5, CATEGORY: 1, RECORD: 2, WORKOUT: 1, ACTIVITY_SUMMARY: 1}
    assert sum(parser.counts.values()) == len(records)
    assert parser.skipped == {"ExportDate": 1, "Me": 1, "ClinicalRecord": 1, "Audiogram": 1}
    assert all(item.channel == EXPORT for item in records)
    found = by_type(records)

    steps = found["HKQuantityTypeIdentifierStepCount"]
    assert steps.kind == QUANTITY
    assert steps.body["value"] == "120" and steps.body["unit"] == "count"
    assert "metadata" not in steps.body and "children" not in steps.body
    assert steps.start == datetime(2026, 11, 6, 9, 0, tzinfo=UTC)
    assert steps.end == datetime(2026, 11, 6, 9, 5, tzinfo=UTC)
    assert steps.source_name == WATCH and steps.device == "Apple Watch"
    # Export samples are immutable: the key is the body's digest.
    assert steps.key == steps.digest == body_digest(steps.body)
    # Every attribute as received, escaped text decoded.
    assert steps.body["device"].startswith("<<HKDevice: 0x600000001>, name:Apple Watch")

    heart = found["HKQuantityTypeIdentifierHeartRate"]
    assert heart.body["metadata"] == {"HKMetadataKeyHeartRateMotionContext": "0"}

    hrv = found["HKQuantityTypeIdentifierHeartRateVariabilitySDNN"]
    assert hrv.body["beats"] == [
        {"bpm": "61", "time": "10:01:02.11 PM"},
        {"bpm": "63", "time": "10:01:03.05 PM"},
    ]
    assert "children" not in hrv.body

    sleep = found["HKCategoryTypeIdentifierSleepAnalysis"]
    assert sleep.kind == CATEGORY
    assert sleep.body["value"] == "HKCategoryValueSleepAnalysisAsleepCore"

    goal = found["HKDataTypeSleepDurationGoal"]
    assert goal.kind == RECORD

    pressure = found["HKCorrelationTypeIdentifierBloodPressure"]
    assert pressure.kind == RECORD and pressure.source_name == PHONE
    systolic = found["HKQuantityTypeIdentifierBloodPressureSystolic"]
    diastolic = found["HKQuantityTypeIdentifierBloodPressureDiastolic"]
    assert pressure.body["records"] == [systolic.digest, diastolic.digest]
    assert pressure.body["metadata"] == {"HKWasUserEntered": "1"}
    assert "children" not in pressure.body
    assert systolic.body["value"] == "120" and diastolic.body["value"] == "80"
    assert systolic.device is None

    run = found["HKWorkoutActivityTypeRunning"]
    assert run.kind == WORKOUT and run.key == run.digest
    assert run.body["duration"] == "30.5" and run.body["totalDistance"] == "5.1"
    assert run.body["metadata"] == {"HKIndoorWorkout": "0", "HKAverageMETs": "9.5 kcal/hr·kg"}
    assert [event["type"] for event in run.body["events"]] == [
        "HKWorkoutEventTypeSegment",
        "HKWorkoutEventTypePause",
    ]
    assert run.body["statistics"][0] == {
        "type": "HKQuantityTypeIdentifierActiveEnergyBurned",
        "startDate": "2026-11-06 17:00:00 +0100",
        "endDate": "2026-11-06 17:30:30 +0100",
        "sum": "250",
        "unit": "kcal",
    }
    assert run.body["routes"] == ["/workout-routes/route_2026-11-06_5.00pm.gpx"]
    # The route element itself is kept whole, its metadata included.
    (route,) = run.body["children"]
    assert route["tag"] == "WorkoutRoute"
    assert [child["tag"] for child in route["children"]] == ["MetadataEntry", "FileReference"]
    assert run.start == datetime(2026, 11, 6, 16, 0, tzinfo=UTC)
    assert run.end == datetime(2026, 11, 6, 16, 30, 30, tzinfo=UTC)

    summary = found["ActivitySummary"]
    assert summary.kind == ACTIVITY_SUMMARY
    assert summary.key == identity_key("ActivitySummary", "2026-11-06")
    assert summary.body["activeEnergyBurned"] == "450.5"
    assert summary.start is None and summary.end is None and summary.source_name is None


@pytest.mark.parametrize("size", [1, 7, 4096])
def test_chunk_size_never_changes_the_records(size):
    whole = parse(full_export())[1]
    parser, records = parse(full_export(), size)
    assert records == whole
    assert [item.digest for item in records] == [item.digest for item in whole]
    assert parser.skipped == {"ExportDate": 1, "Me": 1, "ClinicalRecord": 1, "Audiogram": 1}


def test_feed_returns_records_as_they_complete():
    data = export_xml(record(value="1"), record(value="2"))
    cut = data.index(b'value="2"')
    parser = ExportParser()
    assert [item.body["value"] for item in parser.feed(data[:cut])] == ["1"]
    assert parser.feed(b"") == []
    # expat may defer a partial token until more input (or the end) arrives.
    rest = parser.feed(data[cut:]) + parser.close()
    assert [item.body["value"] for item in rest] == ["2"]


def test_same_export_twice_gives_the_same_keys_and_a_changed_summary_keeps_its_key():
    first = by_type(parse(full_export())[1])
    second = by_type(parse(full_export())[1])
    assert {name: item.key for name, item in first.items()} == {
        name: item.key for name, item in second.items()
    }
    old = parse(export_xml(activity_summary(energy="100")))[1][0]
    new = parse(export_xml(activity_summary(energy="450")))[1][0]
    assert old.key == new.key and old.digest != new.digest


def test_unknown_nested_elements_and_odd_metadata_are_kept():
    data = export_xml(
        record(
            children=[
                *metadata(a="1"),
                element("MetadataEntry", {"key": "a", "value": "2"}),
                element("MetadataEntry", {"value": "no key"}),
                element("FutureElement", {"x": "1"}, element("Inner", {"y": "2"})),
            ]
        ),
        workout(route=None, totals=False).replace(
            "</Workout>",
            '<WorkoutActivity uuid="u1" startDate="s"><WorkoutStatistics type="t" sum="1"/>'
            "</WorkoutActivity></Workout>",
        ),
    )
    records = parse(data)[1]
    steps, run = records
    assert steps.body["metadata"] == {"a": "1"}
    assert steps.body["children"] == [
        {"key": "a", "value": "2", "tag": "MetadataEntry"},
        {"value": "no key", "tag": "MetadataEntry"},
        {"x": "1", "tag": "FutureElement", "children": [{"y": "2", "tag": "Inner"}]},
    ]
    assert "totalDistance" not in run.body and run.body["routes"] == []
    assert run.body["children"] == [
        {
            "uuid": "u1",
            "startDate": "s",
            "tag": "WorkoutActivity",
            "children": [{"type": "t", "sum": "1", "tag": "WorkoutStatistics"}],
        }
    ]


def test_skipped_subtrees_do_not_leak_into_records():
    data = export_xml(
        element("ClinicalRecord", {"type": "x"}, record(value="999")),
        element("VisionPrescription", {"type": "y"}),
        record(value="1"),
    )
    parser, records = parse(data)
    assert [item.body["value"] for item in records] == ["1"]
    assert parser.skipped["ClinicalRecord"] == 1 and parser.skipped["VisionPrescription"] == 1


def test_no_doctype_and_a_bom_are_fine():
    data = b"\xef\xbb\xbf" + export_xml(blood_pressure(), doctype=False)
    parser, records = parse(data, 3)
    assert parser.counts == {QUANTITY: 2, RECORD: 1}


def test_unparseable_dates_are_none_not_errors():
    data = export_xml(record(start="yesterday", end="2026-11-06 10:00:00"))
    item = parse(data)[1][0]
    assert item.start is None and item.end is None  # no offset: not guessed


BILLION_LAUGHS = b"""<?xml version="1.0"?>
<!DOCTYPE HealthData [
<!ENTITY lol "lol">
<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
<!ENTITY lol4 "&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;">
<!ENTITY lol5 "&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;">
<!ENTITY lol6 "&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;">
<!ENTITY lol7 "&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;">
<!ENTITY lol8 "&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;">
<!ENTITY lol9 "&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;">
]>
<HealthData locale="en_US"><Record type="&lol9;" sourceName="x" startDate="a" endDate="b"/>
</HealthData>
"""


@pytest.mark.parametrize("size", [None, 16])
def test_entity_declarations_are_refused_before_any_expansion(size):
    with pytest.raises(ExportFormatError, match="declares an entity"):
        parse(BILLION_LAUGHS, size)


@pytest.mark.parametrize(
    "doctype",
    [
        b'<!DOCTYPE HealthData [<!ENTITY ext SYSTEM "file:///etc/passwd">]>',
        b'<!DOCTYPE HealthData [<!ENTITY % ext SYSTEM "http://example.invalid/x.dtd"> %ext;]>',
        b'<!DOCTYPE HealthData [<!ENTITY img SYSTEM "x.png" NDATA png>]>',
    ],
)
def test_external_entities_are_refused(doctype):
    data = b'<?xml version="1.0"?>\n' + doctype + b'\n<HealthData locale="x"></HealthData>'
    with pytest.raises(ExportFormatError, match="entity"):
        parse(data)


@pytest.mark.parametrize(
    "doctype",
    [
        b'<!DOCTYPE HealthData SYSTEM "http://example.invalid/x.dtd">',
        b'<!DOCTYPE HealthData PUBLIC "-//X//Y" "x.dtd" [<!ELEMENT HealthData ANY>]>',
    ],
)
def test_an_external_dtd_is_refused_not_fetched(doctype):
    data = b'<?xml version="1.0"?>\n' + doctype + b'\n<HealthData locale="x"></HealthData>'
    with pytest.raises(ExportFormatError, match="external DTD"):
        parse(data)


def test_undeclared_entities_are_refused():
    data = export_xml(record()).replace(b"</HealthData>", b"&undeclared;</HealthData>")
    with pytest.raises(ExportFormatError, match="line"):
        parse(data)


def test_another_root_is_not_an_export():
    with pytest.raises(ExportFormatError, match="root element is <ClinicalDocument>"):
        parse(b'<?xml version="1.0"?><ClinicalDocument><Record type="x"/></ClinicalDocument>')


@pytest.mark.parametrize(
    "data, where",
    [
        (export_xml(record())[:-30], "line"),
        (export_xml(record()).replace(b"<Record ", b"<Record <", 1), "line"),
        (b"", "empty|line"),
        (export_xml(record()) + b"<HealthData/>", "line"),
    ],
)
def test_malformed_or_truncated_input_raises_with_a_position(data, where):
    parser = ExportParser()
    with pytest.raises(ExportFormatError, match=where):
        for chunk in chunked(data, 100) if data else [b""]:
            parser.feed(chunk)
        parser.close()


def test_close_is_idempotent_and_feed_after_close_fails():
    parser = ExportParser()
    assert len(parser.feed(export_xml(record()))) == 1
    assert parser.close() == []
    assert parser.close() == []
    with pytest.raises(ExportFormatError, match="closed"):
        parser.feed(b"<x/>")
