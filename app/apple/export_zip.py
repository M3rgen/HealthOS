"""Find and decompress ``export.xml`` inside the Health app's ``export.zip``.

The upload arrives as a stream in order, not as a seekable file, so ``ZipExportReader``
reads the zip's local file headers as they come (the central directory at the end is never
needed): deflated entries (method 8) are inflated until their stream ends, so their sizes
need not be known and a trailing data descriptor is skipped; stored entries (method 0)
need their sizes in the local header (zip64 extra field included). Encrypted entries are
refused.

The main file is the first entry whose name ends in ``.xml``, is not ``export_cda.xml``
(the clinical documents) and whose first bytes (up to 64 KiB) hold ``<HealthData`` or
``<!DOCTYPE HealthData``: its name is localized in some languages, so it is recognised by
content (``apple_health_export/export.xml`` in English). Every other entry (workout route
GPX files, electrocardiogram CSVs, ...) is read past and discarded. Decompressed output is
limited against zip bombs (``ExportTooLarge``): in total (``max_output``, 16 GiB by default,
``APPLE_IMPORT_MAX_UNCOMPRESSED``) and in its ratio to the zip bytes read so far
(``max_ratio``, 100:1, checked once the output passes ``RATIO_GRACE``). Real exports compress
about 10-20:1; a deflate bomb reaches about 1000:1 and is refused within its first 64 MiB.

``open_export_path`` is the CLI's way in: a zip on disk (seekable, read with ``zipfile``,
the same member choice) or a bare ``export.xml``, yielded in chunks.
"""

from __future__ import annotations

import struct
import zipfile
import zlib
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

from app.apple.export_xml import ExportFormatError

LOCAL_HEADER = b"PK\x03\x04"
CENTRAL_HEADER = b"PK\x01\x02"
END_RECORDS = (b"PK\x05\x06", b"PK\x06\x06", b"PK\x06\x07", b"PK\x06\x08")
DATA_DESCRIPTOR = b"PK\x07\x08"
PROBE_BYTES = 64 * 1024
DEFAULT_MAX_OUTPUT = 16 * 1024**3
# Most decompressed bytes per zip byte read; real exports are about 10-20:1, deflate ~1000:1.
MAX_RATIO = 100
# Output below this is never refused for its ratio (a small, very regular head is fine).
RATIO_GRACE = 64 * 1024**2
# Decompressed output is produced in pieces of at most this size.
PIECE = 256 * 1024
_HEADER = struct.Struct("<4sHHHHHIIIHH")
_ZIP64_EXTRA = 0x0001
_MARKERS = (b"<HealthData", b"<!DOCTYPE HealthData")
_CDA = "export_cda.xml"


class ExportTooLarge(ExportFormatError):
    """The zip decompresses to more than allowed (``reason``: ``"output"`` past
    ``max_output`` bytes, ``"ratio"`` past ``max_ratio`` output bytes per zip byte)."""

    def __init__(self, message: str, *, reason: str, limit: int) -> None:
        super().__init__(message)
        self.reason = reason
        self.limit = limit


class OutputGuard:
    """Counts decompressed bytes against the total and ratio limits."""

    __slots__ = ("max_output", "max_ratio", "grace", "output")

    def __init__(
        self,
        max_output: int = DEFAULT_MAX_OUTPUT,
        max_ratio: int = MAX_RATIO,
        grace: int = RATIO_GRACE,
    ) -> None:
        self.max_output = max_output
        self.max_ratio = max_ratio
        self.grace = grace
        self.output = 0

    def add(self, produced: int, compressed: int) -> None:
        """Count ``produced`` more output bytes, ``compressed`` zip bytes read so far."""
        self.output += produced
        if self.output > self.max_output:
            raise ExportTooLarge(
                f"the zip decompresses to more than {self.max_output} bytes; refused",
                reason="output",
                limit=self.max_output,
            )
        if self.output > self.grace and self.output > self.max_ratio * max(compressed, 1):
            raise ExportTooLarge(
                f"the zip decompresses more than {self.max_ratio} times its size "
                "(a zip bomb?); refused",
                reason="ratio",
                limit=self.max_ratio,
            )


def is_candidate_name(name: str) -> bool:
    """Could this entry be the main file (an ``.xml`` that is not ``export_cda.xml``)?"""
    base = PurePosixPath(name.replace("\\", "/")).name
    return base.lower().endswith(".xml") and base.lower() != _CDA


def looks_like_export(head: bytes) -> bool:
    """Do the first bytes of an ``.xml`` entry belong to a Health export?"""
    return any(marker in head for marker in _MARKERS)


class _Entry:
    __slots__ = (
        "name",
        "method",
        "descriptor",
        "zip64",
        "compressed",
        "remaining",
        "consumed",
        "inflater",
        "candidate",
        "skip",
        "probe",
        "selected",
        "crc",
        "expected_crc",
    )

    def __init__(self, name: str, method: int, flags: int, crc: int) -> None:
        self.name = name
        self.method = method
        self.descriptor = bool(flags & 0x08)
        self.zip64 = False
        self.compressed = 0
        self.remaining = 0
        self.consumed = 0
        self.inflater = zlib.decompressobj(-15) if method == 8 else None
        self.candidate = is_candidate_name(name)
        # Read past by size without inflating (an entry that cannot be the export).
        self.skip = False
        self.probe: list[bytes] | None = [] if self.candidate else None
        self.selected = False
        self.crc = 0
        self.expected_crc = crc


class ZipExportReader:
    """Push reader of a zip stream: ``feed(data)`` returns the bytes of the export's main
    member decompressed so far, ``close()`` the rest and checks the member was complete.
    ``member_name`` names the chosen member once it is known."""

    def __init__(
        self,
        max_output: int = DEFAULT_MAX_OUTPUT,
        max_ratio: int = MAX_RATIO,
        ratio_grace: int = RATIO_GRACE,
    ) -> None:
        self.max_output = max_output
        self.member_name: str | None = None
        self.input_bytes = 0
        self._guard = OutputGuard(max_output, max_ratio, ratio_grace)
        self._buffer = bytearray()
        self._entry: _Entry | None = None
        # header | data | descriptor | done (the member is complete; the rest is ignored)
        self._state = "header"
        self._member_done = False
        self._closed = False

    # -- public --------------------------------------------------------------------------

    def feed(self, data: bytes) -> bytes:
        return b"".join(self.iter_feed(data))

    def close(self) -> bytes:
        return b"".join(self.iter_close())

    def iter_feed(self, data: bytes) -> Iterator[bytes]:
        """Like ``feed``, yielding the output in pieces of at most 256 KiB (bounded memory
        however well the chunk compresses)."""
        if self._closed:
            raise ExportFormatError("zip reader is already closed")
        self.input_bytes += len(data)
        if self._state == "done":
            return
        self._buffer += data
        yield from self._run(final=False)

    def iter_close(self) -> Iterator[bytes]:
        if self._closed:
            return
        self._closed = True
        if self._state != "done":
            yield from self._run(final=True)
        if self._member_done:
            return
        if self._state == "done":
            raise ExportFormatError("no Apple Health export.xml in the zip")
        if self.member_name is None:
            raise ExportFormatError(
                "the zip ends before an Apple Health export.xml was found (truncated upload?)"
            )
        raise ExportFormatError("the zip ends inside the export.xml member (truncated upload?)")

    # -- state machine -------------------------------------------------------------------

    def _run(self, final: bool) -> Iterator[bytes]:
        while self._state != "done":
            if self._state == "header":
                if not self._header(final):
                    return
            elif self._state == "data":
                progressed = False
                for piece in self._data():
                    progressed = True
                    if piece:
                        yield piece
                if self._state == "data" and not progressed:
                    return
            elif self._state == "descriptor":
                if not self._descriptor(final):
                    return

    def _header(self, final: bool) -> bool:
        buffer = self._buffer
        if len(buffer) < 4:
            if final and buffer:
                raise ExportFormatError("the zip ends inside a file header")
            return False
        signature = bytes(buffer[:4])
        if signature == CENTRAL_HEADER or signature in END_RECORDS:
            # The central directory: every entry has been read.
            self._state = "done"
            self._buffer = bytearray()
            return True
        if signature != LOCAL_HEADER:
            raise ExportFormatError("not a zip file (or a damaged one): no local file header")
        if len(buffer) < _HEADER.size:
            if final:
                raise ExportFormatError("the zip ends inside a file header")
            return False
        (_, _, flags, method, _, _, crc, csize, usize, name_length, extra_length) = (
            _HEADER.unpack_from(buffer)
        )
        total = _HEADER.size + name_length + extra_length
        if len(buffer) < total:
            if final:
                raise ExportFormatError("the zip ends inside a file header")
            return False
        raw_name = bytes(buffer[_HEADER.size : _HEADER.size + name_length])
        name = raw_name.decode("utf-8" if flags & 0x800 else "cp437", errors="replace")
        extra = bytes(buffer[_HEADER.size + name_length : total])
        del buffer[:total]
        if flags & 0x01 or flags & 0x40:
            raise ExportFormatError(f"the zip entry {name[:120]!r} is encrypted; refused")
        entry = _Entry(name, method, flags, crc)
        if usize == 0xFFFFFFFF or csize == 0xFFFFFFFF:
            usize, csize = self._zip64_sizes(extra, usize, csize)
            entry.zip64 = True
        elif _has_zip64(extra):
            entry.zip64 = True
        self._check_method(entry, name, method)
        if not entry.candidate and not entry.descriptor:
            # An entry that cannot be the export, its size known: skip it by size, unread.
            entry.skip = True
            entry.inflater = None
        entry.remaining = csize
        self._entry = entry
        self._state = "data"
        return True

    @staticmethod
    def _check_method(entry: _Entry, name: str, method: int) -> None:
        """Refuse an entry that cannot be streamed; mark one that cannot be read to be skipped."""
        if method == 0:
            if entry.descriptor:
                raise ExportFormatError(
                    f"the zip entry {name[:120]!r} is stored with a data descriptor; "
                    "its size is unknown while streaming"
                )
        elif method != 8:
            # Only deflate and stored can be read; an .xml entry in another method may be
            # the export itself, and any entry without its size cannot be read past.
            if entry.descriptor or entry.candidate:
                raise ExportFormatError(
                    f"the zip entry {name[:120]!r} uses compression method {method}; "
                    "only deflate and stored are supported"
                )
            entry.skip = True

    @staticmethod
    def _zip64_sizes(extra: bytes, usize: int, csize: int) -> tuple[int, int]:
        offset = 0
        while offset + 4 <= len(extra):
            tag, size = struct.unpack_from("<HH", extra, offset)
            body = extra[offset + 4 : offset + 4 + size]
            if tag == _ZIP64_EXTRA:
                position = 0
                if usize == 0xFFFFFFFF and position + 8 <= len(body):
                    (usize,) = struct.unpack_from("<Q", body, position)
                    position += 8
                if csize == 0xFFFFFFFF and position + 8 <= len(body):
                    (csize,) = struct.unpack_from("<Q", body, position)
                return usize, csize
            offset += 4 + size
        raise ExportFormatError("zip64 entry without its zip64 extra field")

    def _data(self) -> Iterator[bytes]:
        entry = self._entry
        if entry is None:
            raise RuntimeError("zip reader state: no current entry")
        if entry.inflater is None or entry.skip:
            yield from self._stored(entry)
        else:
            yield from self._deflated(entry)

    def _stored(self, entry: _Entry) -> Iterator[bytes]:
        take = min(entry.remaining, len(self._buffer))
        if take == 0 and entry.remaining:
            return
        if entry.skip:
            del self._buffer[:take]
            entry.remaining -= take
            take = 0
        while take:
            size = min(take, PIECE)
            chunk = bytes(self._buffer[:size])
            del self._buffer[:size]
            entry.remaining -= size
            take -= size
            yield self._output(entry, chunk)
        yield b""
        if entry.remaining == 0:
            yield from self._finish(entry)

    def _deflated(self, entry: _Entry) -> Iterator[bytes]:
        inflater = entry.inflater
        if inflater is None:
            raise RuntimeError("zip reader state: a deflated entry without its inflater")
        if not self._buffer and not inflater.unconsumed_tail:
            return
        data = bytes(self._buffer)
        self._buffer = bytearray()
        entry.consumed += len(data)
        try:
            while True:
                piece = inflater.decompress(data, PIECE)
                if piece:
                    yield self._output(entry, piece)
                data = inflater.unconsumed_tail
                # A full piece may leave output pending inside zlib even with no input left.
                if inflater.eof or (not data and len(piece) < PIECE):
                    break
        except zlib.error as error:
            raise ExportFormatError(
                f"the zip entry {entry.name[:120]!r} is damaged: {error}"
            ) from None
        yield b""
        if inflater.eof:
            unused = inflater.unused_data
            entry.consumed -= len(unused)
            self._buffer = bytearray(unused) + self._buffer
            yield from self._finish(entry)

    @property
    def output_bytes(self) -> int:
        return self._guard.output

    def _output(self, entry: _Entry, piece: bytes) -> bytes:
        self._guard.add(len(piece), self.input_bytes)
        if entry.selected:
            entry.crc = zlib.crc32(piece, entry.crc)
            return piece
        if entry.probe is None:
            return b""
        entry.probe.append(piece)
        head = b"".join(entry.probe)
        if len(head) < PROBE_BYTES:
            entry.probe = [head]
            return b""
        return self._decide(entry, head)

    def _decide(self, entry: _Entry, head: bytes) -> bytes:
        entry.probe = None
        if not looks_like_export(head[:PROBE_BYTES]):
            return b""
        entry.selected = True
        self.member_name = entry.name
        entry.crc = zlib.crc32(head)
        return head

    def _finish(self, entry: _Entry) -> Iterator[bytes]:
        if entry.probe is not None:
            # A candidate shorter than the probe: decide on what it holds.
            head = b"".join(entry.probe)
            out = self._decide(entry, head)
            if out:
                yield out
        if entry.descriptor:
            self._state = "descriptor"
            return
        self._complete(entry)

    def _complete(self, entry: _Entry) -> None:
        if entry.selected:
            if entry.crc != entry.expected_crc:
                raise ExportFormatError(f"the zip entry {entry.name[:120]!r} fails its CRC check")
            self._member_done = True
            self._state = "done"
            self._buffer = bytearray()
            return
        self._entry = None
        self._state = "header"

    def _descriptor(self, final: bool) -> bool:
        entry = self._entry
        if entry is None:
            raise RuntimeError("zip reader state: no current entry")
        buffer = self._buffer
        # The longest descriptor (24 bytes) and the next signature's first two bytes.
        if len(buffer) < 26 and not final:
            return False
        offset = 4 if buffer[:4] == DATA_DESCRIPTOR else 0
        for wide in (entry.zip64, not entry.zip64):
            size = 8 if wide else 4
            length = offset + 4 + 2 * size
            if len(buffer) < length:
                continue
            crc = struct.unpack_from("<I", buffer, offset)[0]
            compressed = int.from_bytes(buffer[offset + 4 : offset + 4 + size], "little")
            follows = bytes(buffer[length : length + 2])
            if compressed == entry.consumed and follows in (b"PK", b"P", b""):
                entry.expected_crc = crc
                del buffer[:length]
                self._complete(entry)
                return True
        raise ExportFormatError(f"the zip entry {entry.name[:120]!r} has a damaged data descriptor")


def _has_zip64(extra: bytes) -> bool:
    offset = 0
    while offset + 4 <= len(extra):
        tag, size = struct.unpack_from("<HH", extra, offset)
        if tag == _ZIP64_EXTRA:
            return True
        offset += 4 + size
    return False


def _select_member(archive: zipfile.ZipFile) -> zipfile.ZipInfo:
    for info in archive.infolist():
        if info.is_dir() or not is_candidate_name(info.filename):
            continue
        if info.flag_bits & 0x01:
            raise ExportFormatError(f"the zip entry {info.filename[:120]!r} is encrypted; refused")
        with archive.open(info) as member:
            head = member.read(PROBE_BYTES)
        if looks_like_export(head):
            return info
    raise ExportFormatError("no Apple Health export.xml in the zip")


def open_export_path(
    path: Path, chunk_size: int = 1 << 20, *, max_output: int = DEFAULT_MAX_OUTPUT
) -> Iterator[bytes]:
    """The bytes of the export's ``export.xml`` from a zip or a bare ``.xml`` on disk, in
    chunks of at most ``chunk_size`` bytes (decompressed). A zip member is held to the same
    limits as an upload (``ExportTooLarge``), its ratio taken against its compressed size."""
    with open(path, "rb") as handle:
        is_zip = handle.read(4) == LOCAL_HEADER
    if not is_zip:
        with open(path, "rb") as handle:
            while chunk := handle.read(chunk_size):
                yield chunk
        return
    try:
        with zipfile.ZipFile(path) as archive:
            info = _select_member(archive)
            guard = OutputGuard(max_output)
            with archive.open(info) as member:
                while chunk := member.read(chunk_size):
                    guard.add(len(chunk), info.compress_size)
                    yield chunk
    except (zipfile.BadZipFile, zlib.error, NotImplementedError, RuntimeError) as error:
        raise ExportFormatError(f"the zip cannot be read: {error}") from None
