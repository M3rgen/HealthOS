"""Hostile ZIPs are generated in memory, never fixtures copied from a real activity."""

import hashlib
import io
import stat
import struct
import zipfile

import pytest
from fit_factory import fit_file
from sqlalchemy import func, select
from support.garmin_files import PREFIX, pull_service, seed_activities, zipped

from app.core.fit import MAX_FIT_BYTES
from app.garmin.activity_zip import MAX_ZIP_BYTES, InvalidActivityFile, extract_fit
from app.models.garmin_files import GarminActivityFile, GarminActivityFileResult


def special_zip(kind):
    buffer = io.BytesIO()
    info = zipfile.ZipInfo("synthetic.fit")
    if kind == "symlink":
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
    if kind == "unsupported-compression":
        info.compress_type = zipfile.ZIP_BZIP2
    with zipfile.ZipFile(buffer, "w") as archive:
        if kind == "zip64":
            with archive.open(info, "w", force_zip64=True) as stream:
                stream.write(fit_file(b""))
        else:
            archive.writestr(info, fit_file(b""))
    data = bytearray(buffer.getvalue())
    if kind == "encrypted":
        struct.pack_into("<H", data, 6, 1)
        central = data.index(b"PK\x01\x02")
        struct.pack_into("<H", data, central + 8, 1)
    if kind == "bad-crc":
        data[30 + len(info.filename)] ^= 1
    if kind == "forged-directory-count":
        central = data.index(b"PK\x01\x02")
        duplicate = data[central:-22]
        data[central:central] = duplicate
        end = len(data) - 22
        struct.pack_into("<I", data, end + 12, len(duplicate) * 2)
    if kind == "understated-size":
        # A bomb that declares a small FIT: expansion stops at the declared size.
        data = bytearray(zipped(fit_file(b"\0" * (MAX_FIT_BYTES - 16))))
        central = data.index(b"PK\x01\x02")
        struct.pack_into("<I", data, 22, 4096)
        struct.pack_into("<I", data, central + 24, 4096)
    if kind == "hidden-local-entry":
        central = data.index(b"PK\x01\x02")
        hidden = data[:central]
        data[central:central] = hidden
        struct.pack_into("<I", data, len(data) - 6, central + len(hidden))
    return bytes(data)


def refused_zip(kind):
    if kind in (
        "symlink",
        "unsupported-compression",
        "encrypted",
        "bad-crc",
        "forged-directory-count",
        "hidden-local-entry",
        "understated-size",
        "zip64",
    ):
        return special_zip(kind)
    return {
        "broken": lambda: b"PK\x03\x04invalid",
        "not-zip": lambda: fit_file(b""),
        "empty": lambda: zipped(entries=0),
        "multiple": lambda: zipped(entries=2),
        "not-fit-extension": lambda: zipped(name="synthetic.txt"),
        "not-fit-content": lambda: zipped(b"not-a-fit-file" * 2),
        "fit-length-mismatch": lambda: zipped(fit_file(b"") + b"trailing"),
        "parent-path": lambda: zipped(name="../synthetic.fit"),
        "absolute-path": lambda: zipped(name="/synthetic.fit"),
        "nested-path": lambda: zipped(name="folder/synthetic.fit"),
        "backslash-path": lambda: zipped(name="folder\\synthetic.fit"),
        "drive-path": lambda: zipped(name="Z:synthetic.fit"),
        "oversize-fit": lambda: zipped(fit_file(b"a" * MAX_FIT_BYTES)),
        "oversize-zip": lambda: b"PK\x03\x04" + b"a" * MAX_ZIP_BYTES,
        "truncated": lambda: zipped()[:-2],
        "trailing": lambda: zipped() + b"trailing",
        "prepended": lambda: b"prefix" + zipped(),
    }[kind]()


@pytest.mark.parametrize(
    "kind, reason",
    [
        ("broken", "archive"),
        ("not-zip", "not_zip"),
        ("empty", "not_zip"),
        ("multiple", "entries"),
        ("not-fit-extension", "entry"),
        ("not-fit-content", "not_fit"),
        ("fit-length-mismatch", "not_fit"),
        ("parent-path", "entry"),
        ("absolute-path", "entry"),
        ("nested-path", "entry"),
        ("backslash-path", "entry"),
        ("drive-path", "entry"),
        ("oversize-fit", "too_large"),
        ("oversize-zip", "too_large"),
        ("truncated", "archive"),
        ("trailing", "archive"),
        ("prepended", "not_zip"),
        ("symlink", "entry"),
        ("unsupported-compression", "entry"),
        ("encrypted", "entry"),
        ("bad-crc", "archive"),
        ("forged-directory-count", "archive"),
        ("hidden-local-entry", "archive"),
        ("understated-size", "archive"),
        ("zip64", "entry"),
    ],
)
async def test_invalid_envelope_writes_only_a_reasoned_invalid_result(
    sessions, tmp_path, kind, reason
):
    await seed_activities(sessions, 1)
    pull, server, _ = pull_service(sessions, tmp_path)
    body = server.bodies[PREFIX + "1"] = refused_zip(kind)
    try:
        result = await pull.activity_files()
        assert (result.invalid, result.failed, result.downloaded, result.stopped) == (
            1,
            0,
            0,
            False,
        )
        async with sessions() as session:
            assert await session.scalar(select(func.count()).select_from(GarminActivityFile)) == 0
            (row,) = (await session.scalars(select(GarminActivityFileResult))).all()
        assert (row.status, row.reason, row.file_id) == ("invalid", reason, None)
        # A body refused while reading (over the wire budget) has no complete digest.
        complete = kind != "oversize-zip"
        assert row.content_sha256 == (hashlib.sha256(body).hexdigest() if complete else None)
        assert row.content_size == (len(body) if complete else None)
    finally:
        await pull.aclose()


@pytest.mark.parametrize("header", [12, 14])
def test_only_the_fit_envelope_is_read(header):
    # Bytes need not be valid message definitions: parsing is outside this issue.
    content = fit_file(b"synthetic-not-parsed", header_size=header)
    assert extract_fit(zipped(content, "SYNTHETIC.FIT")) == content


def test_many_forged_entries_are_rejected_before_zipfile_objects(monkeypatch):
    data = special_zip("forged-directory-count")

    def forbidden(*args, **kwargs):
        pytest.fail("ZipFile must not allocate members before the directory is bounded")

    monkeypatch.setattr(zipfile, "ZipFile", forbidden)
    with pytest.raises(InvalidActivityFile, match="^garmin_activity_file_invalid$"):
        extract_fit(data)


@pytest.mark.parametrize("signature", [False, True])
def test_normal_streaming_zip_writer_descriptors_are_supported(signature):
    class NonSeekable(io.BytesIO):
        def seekable(self):
            return False

        def seek(self, *args):
            raise OSError("synthetic non-seekable writer")

    stream = NonSeekable()
    content = fit_file(b"synthetic-not-parsed")
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("synthetic.fit", content)
    data = bytearray(stream.getvalue())
    if not signature:
        descriptor = data.index(b"PK\x07\x08")
        del data[descriptor : descriptor + 4]
        old_offset = struct.unpack_from("<I", data, len(data) - 6)[0]
        struct.pack_into("<I", data, len(data) - 6, old_offset - 4)
    assert extract_fit(bytes(data)) == content
