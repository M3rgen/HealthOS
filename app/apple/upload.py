"""One uploaded Apple Health export, ``export.zip`` or a bare ``export.xml``, as a stream.

``ExportUpload`` recognises the format from the first bytes, unzips when needed
(``ZipExportReader``) and parses (``ExportParser``) chunk by chunk: nothing is kept in
memory or written to disk beyond the chunk at hand, the current zip header and the record
being built.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

from app.apple.export_xml import ExportFormatError, ExportParser
from app.apple.export_zip import DEFAULT_MAX_OUTPUT, LOCAL_HEADER, PIECE, ZipExportReader
from app.apple.records import AppleRecord

ZIP, XML = "zip", "xml"
_BOM = b"\xef\xbb\xbf"
_XML_STARTS = (b"<?xml", b"<HealthData", b"<!DOCTYPE")
# Enough leading bytes to tell the formats apart.
_DETECT_BYTES = 64


def detect_format(head: bytes) -> str | None:
    """``"zip"``, ``"xml"`` or None (neither) from a file's first bytes."""
    if head.startswith(LOCAL_HEADER):
        return ZIP
    text = head[len(_BOM) :] if head.startswith(_BOM) else head
    text = text.lstrip(b" \t\r\n")
    if text.startswith(_XML_STARTS):
        return XML
    return None


class ExportUpload:
    """Detection, unzipping and parsing of one upload stream.

    ``feed(chunk)`` returns the records completed by the chunk, ``close()`` the rest (and
    raises ``ExportFormatError`` if the export is incomplete or not one). ``iter_feed``
    yields them in batches, one per at most 256 KiB of export.xml, so that a well
    compressed chunk never holds more than a batch of records at once.
    """

    def __init__(self, max_output: int = DEFAULT_MAX_OUTPUT) -> None:
        self.bytes_in = 0
        self.xml_bytes = 0
        self._max_output = max_output
        self._head = bytearray()
        self._format: str | None = None
        self._zip: ZipExportReader | None = None
        self._parser = ExportParser()

    # -- properties ----------------------------------------------------------------------

    @property
    def format(self) -> str | None:
        return self._format

    @property
    def member_name(self) -> str | None:
        return self._zip.member_name if self._zip is not None else None

    @property
    def export_date(self) -> datetime | None:
        return self._parser.export_date

    @property
    def counts(self) -> dict[str, int]:
        return self._parser.counts

    @property
    def skipped(self) -> dict[str, int]:
        return self._parser.skipped

    @property
    def locale(self) -> str | None:
        return self._parser.locale

    # -- feeding -------------------------------------------------------------------------

    def feed(self, chunk: bytes) -> list[AppleRecord]:
        return [record for batch in self.iter_feed(chunk) for record in batch]

    def close(self) -> list[AppleRecord]:
        return [record for batch in self.iter_close() for record in batch]

    def iter_feed(self, chunk: bytes) -> Iterator[list[AppleRecord]]:
        self.bytes_in += len(chunk)
        if self._format is None:
            self._head += chunk
            if len(self._head.lstrip(b" \t\r\n" + _BOM)) < _DETECT_BYTES:
                return
            chunk = self._start()
        yield from self._pass(chunk)

    def iter_close(self) -> Iterator[list[AppleRecord]]:
        if self._format is None:
            yield from self._pass(self._start())
        if self._zip is not None:
            for piece in self._zip.iter_close():
                yield self._xml(piece)
        yield self._parser.close()

    def _start(self) -> bytes:
        head = bytes(self._head)
        self._head = bytearray()
        self._format = detect_format(head)
        if self._format is None:
            raise ExportFormatError("not an Apple Health export (.zip or .xml)")
        if self._format == ZIP:
            self._zip = ZipExportReader(max_output=self._max_output)
        return head

    def _pass(self, chunk: bytes) -> Iterator[list[AppleRecord]]:
        if not chunk:
            return
        if self._zip is not None:
            for piece in self._zip.iter_feed(chunk):
                yield self._xml(piece)
            return
        for offset in range(0, len(chunk), PIECE):
            yield self._xml(chunk[offset : offset + PIECE])

    def _xml(self, piece: bytes) -> list[AppleRecord]:
        self.xml_bytes += len(piece)
        return self._parser.feed(piece)
