"""Bounded FIT binary decoding without an SDK or a profile dependency.

Unknown global messages, native fields and developer bytes are consumed using their
local definitions. The original file is archived separately; only the activity profile
fields needed by the importer are interpreted. Both FIT 1 and FIT 2 are accepted.
Timestamp fields are shared decoding state: even an otherwise ignored message can
set the base for a later compressed timestamp and must use the same wire contract.
"""

import struct
from collections.abc import Iterator
from dataclasses import dataclass

from app.core.fit import MAX_FIT_BYTES, MAX_FIT_MESSAGES


class FitError(ValueError):
    """Invalid, unsupported or over-budget FIT input (messages contain no source data)."""


def crc16(data: bytes) -> int:
    """FIT's CRC-16/ARC, initial value zero."""
    crc = 0
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = (crc >> 1) ^ (0xA001 if crc & 1 else 0)
    return crc


@dataclass(frozen=True)
class Field:
    number: int
    size: int
    base: int


@dataclass(frozen=True)
class Definition:
    global_number: int
    endian: str
    fields: tuple[Field, ...]
    developer_size: int


# Shared native fields -> (scalar byte size, FIT base type including endian flag).
# These affect decoding regardless of whether the global message is projected.
COMMON_FIELDS = {253: (4, 0x86)}  # timestamp: scalar uint32

# Native projection fields use the same contracts as the shared fields above.
# Other fields remain in the file archive without profile interpretation.
PROFILE = {
    0: {  # file_id
        0: (1, 0x00),  # type: enum
        1: (2, 0x84),  # manufacturer: uint16
        2: (2, 0x84),  # product: uint16
        3: (4, 0x8C),  # serial_number: uint32z (zero is missing)
        4: (4, 0x86),  # time_created: uint32
    },
    18: {  # session
        2: (4, 0x86),  # start_time: uint32
        5: (1, 0x00),  # sport: enum
        6: (1, 0x00),  # sub_sport: enum
        7: (4, 0x86),  # total_elapsed_time: uint32
        8: (4, 0x86),  # total_timer_time: uint32
        9: (4, 0x86),  # total_distance: uint32
        11: (2, 0x84),  # total_calories: uint16
    },
    20: {3: (1, 0x02)},  # record: heart_rate uint8
}
# Base id -> (struct code, invalid sentinel). Unknown base ids can still be skipped.
BASE_TYPES = {
    0: ("B", 0xFF),
    1: ("b", 0x7F),
    2: ("B", 0xFF),
    3: ("h", 0x7FFF),
    4: ("H", 0xFFFF),
    5: ("i", 0x7FFFFFFF),
    6: ("I", 0xFFFFFFFF),
    10: ("B", 0),
    11: ("H", 0),
    12: ("I", 0),
    13: ("B", 0xFF),
    14: ("q", 0x7FFFFFFFFFFFFFFF),
    15: ("Q", 0xFFFFFFFFFFFFFFFF),
    16: ("Q", 0),
}


def _value(data: bytes, field: Field, endian: str) -> int | None:
    base = BASE_TYPES.get(field.base & 0x1F)
    if base is None or struct.calcsize(base[0]) != field.size:
        return None  # Arrays and noninteger fields are not activity scalars.
    value = struct.unpack(endian + base[0], data)[0]
    return None if value == base[1] else value


def _validate_profile(global_number: int, fields: tuple[Field, ...]) -> None:
    """Validate shared decoding state as well as the activity projection.

    Unknown globals can advance the compressed time base, so their timestamp
    definitions must be checked even when no data message follows. Checking the
    full base byte also rejects a missing endian flag; checking size excludes
    arrays before scalar/sentinel decoding can silently ignore them.
    """
    profile = PROFILE.get(global_number, {})
    for field in fields:
        expected = COMMON_FIELDS.get(field.number, profile.get(field.number))
        if expected is not None and (field.size, field.base) != expected:
            raise FitError("FIT alanının türü veya skaler boyutu geçersiz.")


class Reader:
    def __init__(self, data: bytes, position: int, end: int) -> None:
        self.data, self.position, self.end = data, position, end

    def take(self, size: int) -> bytes:
        stop = self.position + size
        if stop > self.end:
            raise FitError("FIT mesajı eksik.")
        found = self.data[self.position : stop]
        self.position = stop
        return found

    def byte(self) -> int:
        return self.take(1)[0]

    def definition(self, header: int) -> Definition:
        reserved, architecture = self.byte(), self.byte()
        if reserved or architecture not in (0, 1):
            raise FitError("FIT mesaj tanımı geçersiz.")
        endian = "<" if architecture == 0 else ">"
        global_number = struct.unpack(endian + "H", self.take(2))[0]
        fields = tuple(Field(*self.take(3)) for _ in range(self.byte()))
        if len({field.number for field in fields}) != len(fields) or any(
            field.size == 0 for field in fields
        ):
            raise FitError("FIT alan tanımı geçersiz.")
        _validate_profile(global_number, fields)
        developer_size = 0
        if header & 0x20:
            for _ in range(self.byte()):
                _, size, _ = self.take(3)
                if not size:
                    raise FitError("FIT geliştirici alanı geçersiz.")
                developer_size += size
        return Definition(global_number, endian, fields, developer_size)

    def values(self, definition: Definition, compressed: bool) -> dict[int, int | None]:
        values = {}
        wanted = PROFILE.get(definition.global_number, {}).keys() | COMMON_FIELDS.keys()
        for field in definition.fields:
            if compressed and field.number == 253:
                continue
            data = self.take(field.size)
            if field.number in wanted:
                values[field.number] = _value(data, field, definition.endian)
        self.take(definition.developer_size)
        return values


def _reader(data: bytes) -> Reader:
    if len(data) > MAX_FIT_BYTES:
        raise FitError("FIT dosyası çok büyük.")
    if len(data) < 14 or data[0] not in (12, 14) or data[8:12] != b".FIT":
        raise FitError("FIT dosya başlığı geçersiz.")
    if data[1] >> 4 not in (1, 2):
        raise FitError("FIT protokol sürümü desteklenmiyor.")
    size = struct.unpack("<I", data[4:8])[0]
    end = data[0] + size
    if len(data) != end + 2:
        raise FitError("FIT dosya uzunluğu geçersiz.")
    if data[0] == 14:
        header_crc = struct.unpack("<H", data[12:14])[0]
        if header_crc and header_crc != crc16(data[:12]):
            raise FitError("FIT başlık sağlama toplamı geçersiz.")
    if struct.unpack("<H", data[end:])[0] != crc16(data[:end]):
        raise FitError("FIT dosya sağlama toplamı geçersiz.")
    return Reader(data, data[0], end)


def messages(data: bytes) -> Iterator[tuple[int, dict[int, int | None]]]:
    """Yield activity fields in wire order, validating even ignored messages to EOF.

    A compressed timestamp replaces the first uint32 timestamp field and uses the
    last full timestamp's upper bits, rolling forward at the 5-bit boundary.
    """
    reader = _reader(data)
    definitions: dict[int, Definition] = {}
    timestamp: int | None = None
    count = 0
    while reader.position < reader.end:
        count += 1
        if count > MAX_FIT_MESSAGES:
            raise FitError("FIT mesaj sınırı aşıldı.")
        header = reader.byte()
        compressed = bool(header & 0x80)
        if not compressed and header & 0x10:
            raise FitError("FIT mesaj başlığı geçersiz.")
        local = (header >> 5) & 3 if compressed else header & 0x0F
        if not compressed and header & 0x40:
            definitions[local] = reader.definition(header)
            continue
        if not compressed and header & 0x20:
            raise FitError("FIT veri başlığı geçersiz.")
        definition = definitions.get(local)
        if definition is None:
            raise FitError("FIT yerel mesaj tanımı bulunamadı.")
        if compressed and (
            timestamp is None
            or not definition.fields
            or definition.fields[0] != Field(253, 4, 0x86)
        ):
            raise FitError("FIT sıkıştırılmış zaman damgası geçersiz.")
        values = reader.values(definition, compressed)
        if compressed and timestamp is not None:
            offset = header & 0x1F
            values[253] = (timestamp & ~0x1F) + offset + (32 if offset < timestamp & 0x1F else 0)
        moment = values.get(253)
        if moment is not None:
            timestamp = moment
        yield definition.global_number, values
