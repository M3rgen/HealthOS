"""Binary format boundaries and FIT units use only independently written synthetic bytes."""

import struct

import pytest
from fit_factory import STAMP, START, activity, activity_messages, checksum, definition, fit_file

from app.core.fit import FIT_EXERCISE, FIT_HEART_RATE
from app.services.providers import fit_decode, fit_records
from app.services.providers.fit_decode import FitError, crc16, messages
from app.services.providers.fit_records import parse_fit


@pytest.mark.parametrize("data, expected", [(b"", 0), (b"123456789", 0xBB3D)])
def test_crc_known_vectors(data, expected):
    assert crc16(data) == checksum(data) == expected


@pytest.mark.parametrize(
    "size, protocol, header_crc", [(12, 0x10, False), (14, 0x10, False), (14, 0x20, True)]
)
def test_supported_headers_and_protocols(size, protocol, header_crc):
    parsed = parse_fit(
        fit_file(activity_messages(), header_size=size, protocol=protocol, header_crc=header_crc)
    )
    assert len(parsed.records[FIT_EXERCISE]) == 1
    assert len(parsed.records[FIT_HEART_RATE]) == 2


@pytest.mark.parametrize("index", [1, 4, 8, 12, 20, -1])
def test_corrupted_header_payload_or_crc_fails(index):
    data = bytearray(activity())
    data[index] ^= 1
    with pytest.raises(FitError):
        parse_fit(bytes(data))


def test_compressed_timestamp_rollover_and_definition_replacement():
    data = activity_messages()
    data += definition(2, 20, [(253, 4, 0x86), (3, 1, 2)])
    data += b"\x02" + struct.pack("<IB", STAMP + 31, 130)
    data += bytes([0xC0 | ((STAMP + 32) & 31), 131])
    points = parse_fit(fit_file(data)).records[FIT_HEART_RATE]
    assert points[-1][FIT_HEART_RATE.json_key] == {
        "sampleTime": {"physicalTime": (START.replace(second=32)).isoformat()},
        "beatsPerMinute": 131,
    }


def test_unknown_messages_native_arrays_and_developer_fields_stay_aligned():
    data = activity_messages(developer=True)
    # Local 15 unknown message and native fields of unknown base types/array sizes.
    data += definition(15, 999, [(0, 6, 0x84), (1, 4, 0x88), (2, 3, 7)], developer=[(5, 4, 1)])
    data += b"\x0f" + b"\xff" * 13 + b"ABCD"
    decoded = list(messages(fit_file(data)))
    assert decoded[-1] == (999, {})
    assert len(parse_fit(fit_file(data)).records[FIT_HEART_RATE]) == 2


# Independent wire contracts for every field used in identities or health projections.
SCALARS = [
    (0, (0, 1, 0x00), 0xFF),
    (0, (1, 2, 0x84), 0xFFFF),
    (0, (2, 2, 0x84), 0xFFFF),
    (0, (3, 4, 0x8C), 0),
    (0, (4, 4, 0x86), 0xFFFFFFFF),
    (18, (2, 4, 0x86), 0xFFFFFFFF),
    (18, (5, 1, 0x00), 0xFF),
    (18, (6, 1, 0x00), 0xFF),
    (18, (7, 4, 0x86), 0xFFFFFFFF),
    (18, (8, 4, 0x86), 0xFFFFFFFF),
    (18, (9, 4, 0x86), 0xFFFFFFFF),
    (18, (11, 2, 0x84), 0xFFFF),
    (18, (253, 4, 0x86), 0xFFFFFFFF),
    (20, (3, 1, 0x02), 0xFF),
    (20, (253, 4, 0x86), 0xFFFFFFFF),
]


@pytest.mark.parametrize("endian", ["<", ">"])
@pytest.mark.parametrize("kind, field, sentinel", SCALARS)
@pytest.mark.parametrize("mismatch", ["base", "width", "array", "float", "unknown", "flag"])
def test_projection_field_contract_rejects_incompatible_definitions(
    kind, field, sentinel, endian, mismatch
):
    number, size, base = field
    wrong = {
        "base": (size, {0: 2, 2: 1, 0x84: 0x83, 0x86: 0x85, 0x8C: 0x86}[base]),
        "width": (size + 1, base),
        "array": (size * 2, base),
        "float": (4, 0x88),
        "unknown": (size, 0x1F),
        "flag": (size, base ^ 0x80),
    }[mismatch]
    # Definition alone must fail too, before optional/sentinel handling or data decoding.
    tail = definition(3, kind, [(number, *wrong)], endian=endian)
    for payload in (b"", b"\x03" + b"\xff" * wrong[0]):
        with pytest.raises(FitError, match="^FIT alanının türü veya skaler boyutu geçersiz\\.$"):
            list(messages(fit_file(activity_messages(endian=endian) + tail + payload)))


@pytest.mark.parametrize("endian", ["<", ">"])
@pytest.mark.parametrize("kind, field, sentinel", SCALARS)
def test_projection_scalar_sentinels_remain_missing(kind, field, sentinel, endian):
    number, size, _ = field
    payload = sentinel.to_bytes(size, "little" if endian == "<" else "big")
    data = definition(0, kind, [field], endian=endian) + b"\x00" + payload
    assert list(messages(fit_file(data))) == [(kind, {number: None})]


def test_unknown_fields_do_not_inherit_projection_contracts_and_stay_aligned():
    data = activity_messages()
    data += definition(3, 999, [(3, 1, 1)]) + b"\x03\xff"
    data += definition(2, 20, [(253, 4, 0x86), (99, 3, 0x1F), (3, 1, 2)], developer=[(3, 2, 0)])
    data += b"\x02" + struct.pack("<I", STAMP + 2) + b"abc\xfeXY"
    data += b"\x02" + struct.pack("<I", STAMP + 3) + b"def\x00ZZ"
    points = parse_fit(fit_file(data)).records[FIT_HEART_RATE]
    assert [point[FIT_HEART_RATE.json_key]["beatsPerMinute"] for point in points] == [
        120,
        121,
        254,
        0,
    ]


@pytest.mark.parametrize(
    "tail",
    [
        b"\x04",  # undefined local message
        b"\x10",  # reserved message header bit
        b"\x20",  # developer bit on data header
        b"\x40\x00\x02\x00\x00\x00",  # bad architecture
        b"\x40\x01\x00\x00\x00\x00",  # nonzero reserved definition byte
        definition(0, 999, [(0, 0, 2)]),  # empty native field
        definition(0, 999, [(0, 1, 2), (0, 1, 2)]),  # duplicate native field
        definition(0, 999, [], developer=[(0, 0, 0)]),
        definition(0, 999, [(0, 4, 0x86)]) + b"\x00\x01",  # truncated ignored field
        definition(0, 20, [(3, 1, 2)]) + b"\x80\x64",  # compressed without timestamp field
    ],
)
def test_malformed_message_definitions_and_truncated_fields(tail):
    with pytest.raises(FitError):
        parse_fit(fit_file(activity_messages() + tail))


def test_compressed_timestamp_requires_a_previous_full_timestamp():
    data = definition(0, 20, [(253, 4, 0x86), (3, 1, 2)]) + b"\x80\x64"
    with pytest.raises(FitError, match="zaman damgası"):
        parse_fit(fit_file(data))


def test_invalid_heart_rate_sentinel_is_not_a_sample():
    data = activity_messages()
    data += b"\x02" + struct.pack("<IB", STAMP + 2, 255)
    parsed = parse_fit(fit_file(data))
    assert len(parsed.records[FIT_HEART_RATE]) == 2


def test_invalid_or_absent_record_timestamp_is_rejected():
    data = activity_messages()
    data += b"\x02" + struct.pack("<IB", 0xFFFFFFFF, 100)
    with pytest.raises(FitError, match="zaman damgası"):
        parse_fit(fit_file(data))


@pytest.mark.parametrize("manufacturer", [1, 2, 255])
def test_only_activity_files_are_accepted(manufacturer):
    data = bytearray(activity_messages(manufacturer=manufacturer))
    # First definition is 21 bytes, followed by local-header, type, manufacturer.
    data[22] = 5
    with pytest.raises(FitError, match="^FIT etkinlik dosyası gerekli\\.$"):
        parse_fit(fit_file(bytes(data)))


def test_duplicate_or_missing_file_id_and_empty_activity_are_rejected():
    for data in (
        activity_messages() * 2,
        b"",
        definition(0, 0, [(0, 1, 0), (1, 2, 0x84)]) + b"\x00\x04\x01\x00",
    ):
        with pytest.raises(FitError):
            parse_fit(fit_file(data))


def test_parser_byte_message_and_record_budgets(monkeypatch):
    monkeypatch.setattr(fit_decode, "MAX_FIT_BYTES", 64)
    with pytest.raises(FitError, match="çok büyük"):
        parse_fit(activity())
    monkeypatch.setattr(fit_decode, "MAX_FIT_BYTES", 8 * 1024 * 1024)
    monkeypatch.setattr(fit_decode, "MAX_FIT_MESSAGES", 2)
    with pytest.raises(FitError, match="mesaj sınırı"):
        parse_fit(activity())
    monkeypatch.setattr(fit_decode, "MAX_FIT_MESSAGES", 100_000)
    monkeypatch.setattr(fit_records, "MAX_FIT_RECORDS", 2)
    with pytest.raises(FitError, match="kayıt sınırı"):
        parse_fit(activity())


def test_session_fallbacks_and_missing_optional_values():
    # File id followed by a session without timestamp: derive end only from elapsed time.
    data = activity_messages()[:35]
    data += definition(1, 18, [(2, 4, 0x86), (7, 4, 0x86), (9, 4, 0x86), (11, 2, 0x84)])
    data += b"\x01" + struct.pack("<IIIH", STAMP, 1000, 0xFFFFFFFF, 0xFFFF)
    parsed = parse_fit(fit_file(data)).records[FIT_EXERCISE][0][FIT_EXERCISE.json_key]
    assert parsed["activeDuration"] == 1
    assert parsed["exerciseType"] == "OTHER"
    assert parsed["metricsSummary"] == {}
    assert parsed["interval"]["endTime"] == START.replace(second=1).isoformat()


def test_session_must_have_a_valid_interval():
    data = activity_messages()[:35]
    data += definition(1, 18, [(2, 4, 0x86), (253, 4, 0x86)])
    data += b"\x01" + struct.pack("<II", STAMP, STAMP - 1)
    with pytest.raises(FitError, match="zaman aralığı"):
        parse_fit(fit_file(data))


@pytest.mark.parametrize("base, value, code", [(0x85, -1, "i"), (0x8F, 2**63, "Q")])
def test_wrong_duration_type_is_rejected_before_interpreting_its_value(base, value, code):
    data = activity_messages()[:35]
    data += definition(1, 18, [(2, 4, 0x86), (7, struct.calcsize(code), base)])
    data += b"\x01" + struct.pack("<I" + code, STAMP, value)
    with pytest.raises(FitError, match="alanının türü veya skaler boyutu"):
        parse_fit(fit_file(data))


def test_missing_serial_uses_stable_file_creation_evidence():
    # A zero uint32z serial is missing, not a shared device named zero.
    first = parse_fit(activity(serial=0))
    equivalent = parse_fit(activity(serial=0, endian=">", developer=True))
    assert (
        first.records[FIT_EXERCISE][0]["dataSource"]
        == equivalent.records[FIT_EXERCISE][0]["dataSource"]
    )
    different_creation = bytearray(activity_messages(serial=0))
    different_creation[31:35] = struct.pack("<I", STAMP + 1)
    other = parse_fit(fit_file(bytes(different_creation)))
    assert (
        first.records[FIT_EXERCISE][0]["dataSource"] != other.records[FIT_EXERCISE][0]["dataSource"]
    )
