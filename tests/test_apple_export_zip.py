"""export.zip streaming, format detection, the upload pipeline, the CLI path reader and a
constant-memory run over a generated ~50 MB export.

Archives are built in memory with ``zipfile`` (or generated on the fly) from synthetic
documents (tests/apple_fixtures.py).
"""

from __future__ import annotations

import struct
import tracemalloc
import zipfile
import zlib

import pytest
from apple_fixtures import (
    chunked,
    export_xml,
    export_zip,
    full_export,
    generated_export,
    record,
    streamed_zip,
    zip_bytes,
)

from app.apple.export_xml import ExportFormatError
from app.apple.export_zip import ZipExportReader, open_export_path
from app.apple.records import QUANTITY
from app.apple.upload import ExportUpload, detect_format

MEMBER = "apple_health_export/export.xml"
CDA = b'<?xml version="1.0"?><ClinicalDocument/>'


def unzip(data: bytes, size: int = 7, **options) -> tuple[bytes, ZipExportReader]:
    reader = ZipExportReader(**options)
    out = b"".join(reader.feed(chunk) for chunk in chunked(data, size)) + reader.close()
    return out, reader


@pytest.mark.parametrize("size", [1, 7, 1000, 1 << 20])
@pytest.mark.parametrize(
    "options",
    [{}, {"streaming": True}, {"compression": zipfile.ZIP_STORED}],
    ids=["deflate", "data-descriptor", "stored"],
)
def test_the_export_member_is_found_and_decompressed(options, size):
    xml = full_export()
    out, reader = unzip(export_zip(xml, **options), size)
    assert out == xml
    assert reader.member_name == MEMBER


@pytest.mark.parametrize(
    "name", ["apple_health_export/exportar.xml", "apple_health_export/Dışa aktarılan.xml"]
)
def test_a_localized_member_name_is_recognised_by_content(name):
    xml = full_export()
    out, reader = unzip(export_zip(xml, name=name))
    assert out == xml and reader.member_name == name


def test_cda_and_other_xml_entries_before_the_export_are_passed_over():
    xml = full_export()
    data = zip_bytes(
        [
            ("apple_health_export/export_cda.xml", b"<HealthData>not this</HealthData>"),
            ("apple_health_export/notes.xml", b'<?xml version="1.0"?><Other/>' + b" " * 70000),
            ("apple_health_export/workout-routes/route_1.gpx", b"<gpx/>" * 1000),
            ("apple_health_export/electrocardiograms/ecg_1.csv", b"Name,Test\n" * 100),
            ("apple_health_export/", b""),
            (MEMBER, xml),
            ("apple_health_export/export2.xml", export_xml(record(value="9"))),
        ]
    )
    for size in (5, 4096):
        out, reader = unzip(data, size)
        assert out == xml and reader.member_name == MEMBER


def test_streaming_zip64_descriptors_are_read():
    xml = full_export()
    data = b"".join(streamed_zip(MEMBER, chunked(xml, 100)))
    out, reader = unzip(data, 3)
    assert out == xml and reader.member_name == MEMBER


def test_a_zip_without_an_export_is_refused():
    data = zip_bytes([("apple_health_export/export_cda.xml", CDA), ("readme.txt", b"hi")])
    with pytest.raises(ExportFormatError, match="no Apple Health export.xml in the zip"):
        unzip(data)


def test_a_truncated_zip_is_refused():
    data = export_zip(full_export())
    with pytest.raises(ExportFormatError, match="truncated"):
        unzip(data[: len(data) // 3])
    head = data.index(MEMBER.encode())
    with pytest.raises(ExportFormatError, match="truncated|header"):
        unzip(data[:head])


def test_encrypted_entries_are_refused():
    data = bytearray(zip_bytes([(MEMBER, full_export())]))
    flags = struct.unpack_from("<H", data, 6)[0]
    struct.pack_into("<H", data, 6, flags | 0x01)
    with pytest.raises(ExportFormatError, match="encrypted"):
        unzip(bytes(data))


def test_stored_with_data_descriptor_and_unknown_methods_are_refused():
    data = bytearray(zip_bytes([(MEMBER, full_export())], compression=zipfile.ZIP_STORED))
    struct.pack_into("<H", data, 6, 0x08)
    with pytest.raises(ExportFormatError, match="data descriptor"):
        unzip(bytes(data))
    data = bytearray(zip_bytes([(MEMBER, full_export())], compression=zipfile.ZIP_STORED))
    struct.pack_into("<H", data, 8, 12)  # bzip2
    with pytest.raises(ExportFormatError, match="method 12"):
        unzip(bytes(data))


def test_a_damaged_member_fails_its_crc_or_inflate():
    data = bytearray(zip_bytes([(MEMBER, full_export())], compression=zipfile.ZIP_STORED))
    at = data.index(b"HKQuantityTypeIdentifierStepCount")
    data[at] ^= 0x01
    with pytest.raises(ExportFormatError, match="CRC"):
        unzip(bytes(data))


def test_decompressed_output_is_limited():
    data = zip_bytes([(MEMBER, export_xml(*(record(value=str(n)) for n in range(50))))])
    with pytest.raises(ExportFormatError, match="more than 4096 bytes"):
        unzip(data, max_output=4096)
    entries = [("apple_health_export/workout-routes/bomb.gpx", b"\0" * (4 << 20))]
    # An entry that cannot be the export and states its size is skipped unread...
    with pytest.raises(ExportFormatError, match="no Apple Health"):
        unzip(zip_bytes(entries), 1000, max_output=1 << 20)
    # ...one with a data descriptor has to be inflated to find its end: limited too.
    with pytest.raises(ExportFormatError, match="more than"):
        unzip(zip_bytes(entries, streaming=True), 1000, max_output=1 << 20)


def test_not_a_zip():
    with pytest.raises(ExportFormatError, match="not a zip"):
        unzip(b"PK\x03\x05" + b"\0" * 40)


def test_iter_feed_bounds_each_output_piece():
    xml = b"".join(generated_export(4000))
    data = zip_bytes([(MEMBER, xml)])
    reader = ZipExportReader()
    pieces = list(reader.iter_feed(data)) + list(reader.iter_close())
    assert b"".join(pieces) == xml
    assert max(len(piece) for piece in pieces) <= 256 * 1024


# -- detection and the upload pipeline ------------------------------------------------------


@pytest.mark.parametrize(
    "head, expected",
    [
        (b"PK\x03\x04rest", "zip"),
        (b'<?xml version="1.0"?>', "xml"),
        (b"\xef\xbb\xbf\n  <?xml", "xml"),
        (b"<HealthData locale", "xml"),
        (b"<!DOCTYPE HealthData", "xml"),
        (b"PK\x05\x06", None),
        (b"%PDF-1.7", None),
        (b"", None),
    ],
)
def test_detect_format(head, expected):
    assert detect_format(head) == expected


def upload(data: bytes, size: int) -> tuple[ExportUpload, list]:
    target = ExportUpload()
    records = []
    for chunk in chunked(data, size):
        records += target.feed(chunk)
    records += target.close()
    return target, records


@pytest.mark.parametrize("size", [1, 50, 1 << 20])
@pytest.mark.parametrize("kind", ["zip", "xml"])
def test_upload_gives_the_same_records_from_a_zip_or_the_xml(kind, size):
    xml = full_export()
    data = export_zip(xml) if kind == "zip" else xml
    target, records = upload(data, size)
    _, direct = upload(xml, 1 << 20)
    assert records == direct and len(records) == 10
    assert target.format == kind
    assert target.member_name == (MEMBER if kind == "zip" else None)
    assert target.bytes_in == len(data) and target.xml_bytes == len(xml)
    assert target.export_date is not None and target.locale == "en_US"
    assert target.counts[QUANTITY] == 5 and target.skipped["Me"] == 1


def test_a_tiny_xml_upload_is_detected_at_close():
    with pytest.raises(ExportFormatError, match="empty|line"):
        upload(b"<?xml ?>", 1)
    target, records = upload(b"<HealthData/>", 2)
    assert records == [] and target.format == "xml"


@pytest.mark.parametrize(
    "data", [b"%PDF-1.7\n" + b"x" * 100, b"", b"   ", b"PK\x05\x06" + b"\0" * 18]
)
def test_upload_of_something_else_is_refused(data):
    with pytest.raises(ExportFormatError, match=r"not an Apple Health export \(.zip or .xml\)"):
        upload(data, 10)


def test_upload_errors_from_the_parser_and_the_zip_surface():
    with pytest.raises(ExportFormatError, match="root element"):
        upload(export_zip(b'<?xml version="1.0"?><HealthDataX/>'), 64)
    with pytest.raises(ExportFormatError, match="no Apple Health"):
        upload(zip_bytes([("a.txt", b"x")]), 64)


# -- the CLI path reader --------------------------------------------------------------------


def test_open_export_path_reads_a_zip_and_a_bare_xml(tmp_path):
    xml = full_export()
    archive = tmp_path / "export.zip"
    archive.write_bytes(export_zip(xml, name="apple_health_export/exportación.xml"))
    bare = tmp_path / "export.xml"
    bare.write_bytes(xml)
    assert b"".join(open_export_path(archive, chunk_size=100)) == xml
    assert b"".join(open_export_path(bare, chunk_size=100)) == xml
    assert max(len(chunk) for chunk in open_export_path(bare, chunk_size=100)) <= 100
    missing = tmp_path / "other.zip"
    missing.write_bytes(zip_bytes([("apple_health_export/export_cda.xml", CDA)]))
    with pytest.raises(ExportFormatError, match="no Apple Health export.xml"):
        list(open_export_path(missing))
    broken = tmp_path / "broken.zip"
    broken.write_bytes(export_zip(xml)[:200])
    with pytest.raises(ExportFormatError, match="cannot be read"):
        list(open_export_path(broken))


# -- constant memory ------------------------------------------------------------------------

# ~49 MB of export.xml (about 376 bytes per record).
RECORDS = 130_000
CHUNK = 1 << 20
PEAK_LIMIT = 32 * 1024 * 1024


def _rechunk(chunks, size):
    buffer = bytearray()
    for chunk in chunks:
        buffer += chunk
        while len(buffer) >= size:
            yield bytes(buffer[:size])
            del buffer[:size]
    if buffer:
        yield bytes(buffer)


@pytest.mark.parametrize("kind", ["xml", "zip"])
def test_a_50_mb_export_streams_in_constant_memory(kind):
    source = generated_export(RECORDS)
    if kind == "zip":
        source = streamed_zip(MEMBER, source)
    target = ExportUpload()
    seen = 0
    tracemalloc.start()
    try:
        for chunk in _rechunk(source, CHUNK):
            for batch in target.iter_feed(chunk):
                seen += len(batch)  # records discarded, as the importer writes and drops them
        for batch in target.iter_close():
            seen += len(batch)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert seen == RECORDS == target.counts[QUANTITY]
    assert target.xml_bytes > 45_000_000
    assert peak < PEAK_LIMIT, f"peak {peak / 1e6:.1f} MB"


def test_zlib_raw_stream_matches_the_generator():
    # The on-the-fly zip writer itself: zipfile reads what it produces.
    xml = b"".join(generated_export(300))
    data = b"".join(streamed_zip(MEMBER, chunked(xml, 1000)))
    import io

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert archive.read(MEMBER) == xml
        assert archive.getinfo(MEMBER).CRC == zlib.crc32(xml)
