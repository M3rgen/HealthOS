"""Synthetic FIT activity writer. No real device, location or health data.

CRC uses the FIT SDK nibble table, independently of the production bitwise decoder.
"""

import struct
from datetime import UTC, datetime

EPOCH = datetime(1989, 12, 31, tzinfo=UTC)
START = datetime(2026, 11, 1, 10, tzinfo=UTC)
STAMP = int((START - EPOCH).total_seconds())
CRC_TABLE = (
    0x0000,
    0xCC01,
    0xD801,
    0x1400,
    0xF001,
    0x3C00,
    0x2800,
    0xE401,
    0xA001,
    0x6C00,
    0x7800,
    0xB401,
    0x5000,
    0x9C01,
    0x8801,
    0x4400,
)


def checksum(data):
    crc = 0
    for byte in data:
        for nibble in (byte & 15, byte >> 4):
            crc = (crc >> 4) ^ CRC_TABLE[crc & 15] ^ CRC_TABLE[nibble]
    return crc


def fit_file(messages, *, header_size=14, protocol=0x20, header_crc=True):
    header = struct.pack("<BBHI4s", header_size, protocol, 2200, len(messages), b".FIT")
    if header_size == 14:
        header += struct.pack("<H", checksum(header) if header_crc else 0)
    content = header + messages
    return content + struct.pack("<H", checksum(content))


def definition(local, global_number, fields, *, endian="<", developer=()):
    header = 0x40 | local | (0x20 if developer else 0)
    output = bytes([header, 0, int(endian == ">")])
    output += struct.pack(endian + "H", global_number) + bytes([len(fields)])
    output += b"".join(bytes(field) for field in fields)
    if developer:
        output += bytes([len(developer)]) + b"".join(bytes(field) for field in developer)
    return output


def activity_messages(
    *,
    bpm=120,
    serial=4242,
    endian="<",
    developer=False,
    distance=250000,
    manufacturer=1,
    elapsed=600000,
    duration=590500,
    sport=1,
):
    data = definition(
        0, 0, [(0, 1, 0), (1, 2, 0x84), (2, 2, 0x84), (3, 4, 0x8C), (4, 4, 0x86)], endian=endian
    )
    data += b"\x00" + struct.pack(endian + "BHHII", 4, manufacturer, 123, serial, STAMP)
    data += definition(
        1,
        18,
        [
            (253, 4, 0x86),
            (2, 4, 0x86),
            (5, 1, 0),
            (6, 1, 0),
            (7, 4, 0x86),
            (8, 4, 0x86),
            (9, 4, 0x86),
            (11, 2, 0x84),
        ],
        endian=endian,
    )
    data += b"\x01" + struct.pack(
        endian + "IIBBIIIH",
        STAMP + elapsed // 1000,
        STAMP,
        sport,
        0,
        elapsed,
        duration,
        distance,
        150,
    )
    data += definition(
        2,
        20,
        [(253, 4, 0x86), (3, 1, 2)],
        endian=endian,
        developer=[(0, 3, 0)] if developer else (),
    )
    data += b"\x02" + struct.pack(endian + "IB", STAMP, bpm)
    if developer:
        data += b"abc"
    data += bytes([0x80 | (2 << 5) | ((STAMP + 1) & 31), bpm + 1])
    if developer:
        data += b"def"
    return data


def activity(**kwargs):
    return fit_file(activity_messages(**kwargs))
