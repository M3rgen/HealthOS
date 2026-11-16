"""Bounded original-file envelopes; no FIT messages, names or values are interpreted.

Inspect the central directory before ZipFile allocates one object per member. A normal,
single-disk, single-member ZIP is sufficient for the original-file endpoint; ZIP64,
concatenated archives and filesystem paths are deliberately refused.
"""

import io
import stat
import struct
import zipfile
import zlib

from app.core.fit import MAX_FIT_BYTES

MAX_ZIP_BYTES = MAX_FIT_BYTES + 128 * 1024
# Why a received original file was refused; stored as a fixed code, never with file content.
NOT_ZIP, ARCHIVE, ENTRIES, ENTRY, TOO_LARGE, NOT_FIT, ENCODING = (
    "not_zip",
    "archive",
    "entries",
    "entry",
    "too_large",
    "not_fit",
    "encoding",  # an unasked-for HTTP content encoding; the body was never read
)
REASONS = (NOT_ZIP, ARCHIVE, ENTRIES, ENTRY, TOO_LARGE, NOT_FIT, ENCODING)


class InvalidActivityFile(ValueError):
    """The message is a fixed code; ``reason`` is one of :data:`REASONS`."""

    def __init__(self, reason: str = ARCHIVE) -> None:
        super().__init__("garmin_activity_file_invalid")
        self.reason = reason


def _directory(data: bytes) -> int:
    if len(data) > MAX_ZIP_BYTES:
        raise InvalidActivityFile(TOO_LARGE)
    if not data.startswith(b"PK\x03\x04"):
        raise InvalidActivityFile(NOT_ZIP)
    end = data.rfind(b"PK\x05\x06", max(0, len(data) - 65557))
    if end < 0 or end + 22 > len(data):
        raise InvalidActivityFile()
    _, disk, directory_disk, entries, total, size, offset, comment = struct.unpack_from(
        "<4s4H2IH", data, end
    )
    if (entries, total) != (1, 1):
        raise InvalidActivityFile(ENTRIES)
    if (disk, directory_disk) != (0, 0):
        raise InvalidActivityFile()
    if end + 22 + comment != len(data) or offset + size != end or size < 46:
        raise InvalidActivityFile()
    if data[offset : offset + 4] != b"PK\x01\x02":
        raise InvalidActivityFile()
    lengths = struct.unpack_from("<3H", data, offset + 28)
    if size != 46 + sum(lengths):
        raise InvalidActivityFile()
    return offset


def _member(info: zipfile.ZipInfo) -> None:
    name = info.orig_filename
    mode = stat.S_IFMT(info.external_attr >> 16)
    if (
        not name.lower().endswith(".fit")
        or any(c in name for c in ("/", "\\", ":", "\x00"))
        or name.startswith(".")
        or info.is_dir()
        or mode not in (0, stat.S_IFREG)
        or info.header_offset != 0
        or info.volume != 0
        or info.flag_bits & 1
        or info.extract_version > 20
        or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
    ):
        raise InvalidActivityFile(ENTRY)
    if info.file_size > MAX_FIT_BYTES or info.compress_size > MAX_ZIP_BYTES:
        raise InvalidActivityFile(TOO_LARGE)
    if info.file_size < 14:
        raise InvalidActivityFile(NOT_FIT)


def _local_entry(data: bytes, info: zipfile.ZipInfo, directory: int) -> None:
    # Nothing may hide between the sole local member and its central directory.
    if len(data) < 30:
        raise InvalidActivityFile()
    version, flags, method = struct.unpack_from("<3H", data, 4)
    crc, compressed, expanded, name, extra = struct.unpack_from("<3I2H", data, 14)
    stop = 30 + name + extra + info.compress_size
    if version > 20 or flags != info.flag_bits or method != info.compress_type:
        raise InvalidActivityFile()
    expected = (info.CRC, info.compress_size, info.file_size)
    if flags & 8:
        # Streaming ZIP writers put CRC/sizes in a descriptor instead of the header.
        start = stop + (4 if data[stop : stop + 4] == b"PK\x07\x08" else 0)
        if start + 12 != directory or struct.unpack_from("<3I", data, start) != expected:
            raise InvalidActivityFile()
    elif stop != directory or (crc, compressed, expanded) != expected:
        raise InvalidActivityFile()


def extract_fit(data: bytes) -> bytes:
    """Check the ZIP envelope and FIT header signature, without decoding messages.

    No filesystem extraction. Read at most the FIT budget plus one sentinel byte;
    both the declared and actual lengths must agree and ZIP's CRC must verify.
    Exceptions contain only a fixed code, never the member name or file bytes.
    """
    try:
        directory = _directory(data)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if len(archive.infolist()) != 1:
                raise InvalidActivityFile(ENTRIES)
            info = archive.infolist()[0]
            _member(info)
            _local_entry(data, info, directory)
            with archive.open(info) as stream:
                content = stream.read(MAX_FIT_BYTES + 1)
            if len(content) != info.file_size or len(content) > MAX_FIT_BYTES:
                raise InvalidActivityFile()
            header = content[0]
            declared = int.from_bytes(content[4:8], "little")
            if (
                header not in (12, 14)
                or content[8:12] != b".FIT"
                or header + declared + 2 != len(content)
            ):
                raise InvalidActivityFile(NOT_FIT)
            return content
    except InvalidActivityFile:
        raise
    except (ValueError, zipfile.BadZipFile, EOFError, OSError, RuntimeError, zlib.error):
        pass
    raise InvalidActivityFile(ARCHIVE)
