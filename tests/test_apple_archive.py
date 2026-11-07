"""The Apple raw archive (app/services/apple_archive.py): versions, payloads, sources and the
import bookkeeping. Synthetic records only."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from apple_fixtures import full_export, hae_body, hae_steps
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.apple.export_xml import ExportParser
from app.apple.hae import parse_hae
from app.apple.records import EXPORT, HAE, QUANTITY, AppleRecord
from app.core.timeutil import as_utc
from app.models.apple import AppleImport, ApplePayload, AppleRawRecord, AppleSource
from app.services import apple_archive
from app.services.apple_archive import (
    import_as_dict,
    kept_headers,
    record_from_row,
    store_payload,
    write_records,
)

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
HEADERS = {
    "Authorization": "Bearer synthetic-secret",
    "automation-name": "Test automation",
    "Automation-Aggregation": "hours",
    "X-Other": "dropped",
}


def export_records():
    parser = ExportParser()
    return parser.feed(full_export()) + parser.close()


def steps(value: str, *, source: str = "Test Watch", device: str | None = None, hour: int = 10):
    start = datetime(2026, 11, 6, hour, tzinfo=UTC)
    return AppleRecord(
        channel=EXPORT,
        kind=QUANTITY,
        type="HKQuantityTypeIdentifierStepCount",
        key="k" * 64,
        body={"value": value},
        start=start,
        end=start + timedelta(minutes=5),
        source_name=source,
        device=device,
    )


async def count(session, model=AppleRawRecord):
    return await session.scalar(select(func.count()).select_from(model))


async def test_write_records_is_idempotent(sessions):
    records = export_records()
    async with sessions.begin() as session:
        new = await write_records(session, records, now=NOW)
    assert new == records
    async with sessions.begin() as session:
        assert await write_records(session, records, now=NOW + timedelta(hours=1)) == []
        assert await write_records(session, export_records()) == []
        assert await count(session) == len(records)
        rows = (await session.scalars(select(AppleRawRecord).order_by(AppleRawRecord.id))).all()
    assert [record_from_row(row) for row in rows] == records
    assert [record_from_row(row).digest for row in rows] == [item.digest for item in records]
    assert all(as_utc(row.first_seen_at) == NOW for row in rows)


async def test_duplicates_inside_one_call_count_once(sessions):
    item = steps("1")
    async with sessions.begin() as session:
        assert await write_records(session, [item, item, steps("1")], now=NOW) == [item]
        assert await write_records(session, []) == []
        assert await count(session) == 1


async def test_a_changed_body_with_the_same_key_is_a_new_version(sessions):
    first, second = steps("1"), steps("2")
    assert first.key == second.key and first.digest != second.digest
    async with sessions.begin() as session:
        await write_records(session, [first], now=NOW)
    async with sessions.begin() as session:
        assert await write_records(session, [first, second], now=NOW) == [second]
        rows = (await session.scalars(select(AppleRawRecord).order_by(AppleRawRecord.id))).all()
    assert [row.body for row in rows] == [{"value": "1"}, {"value": "2"}]
    async with sessions() as session:
        with pytest.raises(DBAPIError, match="refused"):
            await session.execute(text("UPDATE apple_raw_records SET type = 'x'"))
        await session.rollback()


async def test_store_payload_keeps_each_body_once_without_secrets(sessions):
    body = hae_body(metrics=[hae_steps()])
    async with sessions.begin() as session:
        first, stored = await store_payload(session, body, HEADERS, now=NOW)
        again, stored_again = await store_payload(session, body, {}, now=NOW)
        other, stored_other = await store_payload(session, body + b" ", HEADERS)
    assert stored and not stored_again and stored_other
    assert first == again != other
    async with sessions() as session:
        row = await session.get(ApplePayload, first)
        assert row is not None
        assert row.body == body and row.body_size == len(body) and row.channel == HAE
        assert row.headers == {
            "automation-name": "Test automation",
            "automation-aggregation": "hours",
        }
        assert await count(session, ApplePayload) == 2
    assert "authorization" not in kept_headers(HEADERS)
    assert kept_headers({"Session-Id": "s" * 400}) == {"session-id": "s" * 255}


async def test_records_link_to_their_payload_and_import(sessions):
    body = hae_body(metrics=[hae_steps()])
    async with sessions.begin() as session:
        payload_id, _ = await store_payload(session, body, HEADERS)
        import_id = await apple_archive.start_import(session, channel=HAE, origin="receiver")
        await write_records(
            session, parse_hae(body).records, payload_id=payload_id, import_id=import_id
        )
    async with sessions() as session:
        row = await session.scalar(select(AppleRawRecord))
        assert row is not None
        assert (row.payload_id, row.import_id, row.channel) == (payload_id, import_id, HAE)


async def test_sources_are_merged(sessions):
    async with sessions.begin() as session:
        await write_records(session, export_records(), now=NOW)
    async with sessions() as session:
        sources = {
            row.source_name: row for row in (await session.scalars(select(AppleSource))).all()
        }
    assert set(sources) == {"Test Watch", "Test iPhone"}
    watch = sources["Test Watch"]
    assert watch.device == "Apple Watch" and watch.channels == [EXPORT]
    assert "HKWorkoutActivityTypeRunning" in watch.data_types
    assert watch.data_types == sorted(watch.data_types)
    assert as_utc(watch.last_data_at) == datetime(2026, 11, 6, 16, 30, 30, tzinfo=UTC)
    assert as_utc(watch.first_seen_at) == NOW
    phone = sources["Test iPhone"]
    assert phone.device is None and phone.records == 3  # the correlation and its two records

    later = NOW + timedelta(days=1)
    hae = parse_hae(hae_body(metrics=[hae_steps(date="2026-11-08 00:00:00 +0100")])).records
    async with sessions.begin() as session:
        await write_records(session, [steps("old", hour=1), *hae], now=later)
    async with sessions() as session:
        watch = await session.get(AppleSource, "Test Watch")
        assert watch is not None
        await session.refresh(watch)
    assert watch.channels == [EXPORT, HAE]
    assert "step_count" in watch.data_types and "HKQuantityTypeIdentifierStepCount" in (
        watch.data_types
    )
    # A device seen before is kept when newer records name none; the newest data time only
    # moves forward; first_seen stays.
    assert watch.device == "Apple Watch"
    assert as_utc(watch.last_data_at) == datetime(2026, 11, 7, 23, 0, tzinfo=UTC)
    assert as_utc(watch.first_seen_at) == NOW and as_utc(watch.last_seen_at) == later
    assert watch.records == sources["Test Watch"].records + 2


async def test_records_without_a_source_leave_sources_alone(sessions):
    async with sessions.begin() as session:
        await write_records(session, [steps("1", source="  ")], now=NOW)
        assert await count(session, AppleSource) == 0
        assert await count(session) == 1


async def test_import_bookkeeping(sessions):
    async with sessions.begin() as session:
        import_id = await apple_archive.start_import(
            session, channel=EXPORT, origin="upload", filename="  export.zip ", now=NOW
        )
    async with sessions.begin() as session:
        await apple_archive.update_import(session, import_id, bytes_read=100, records_read=7)
    async with sessions() as session:
        item = await session.get(AppleImport, import_id)
        assert item is not None
        await session.refresh(item)
        status = import_as_dict(item)
    assert status["status"] == apple_archive.RUNNING and status["filename"] == "export.zip"
    assert (status["bytes_read"], status["records_read"], status["records_new"]) == (100, 7, 0)
    assert status["started_at"] == NOW.isoformat() and status["finished_at"] is None
    assert status["summary"] == {}

    async with sessions.begin() as session:
        await apple_archive.finish_import(
            session,
            import_id,
            now=NOW + timedelta(minutes=1),
            records_new=5,
            export_date=NOW,
            summary={"counts": {QUANTITY: 7}},
        )
        failed = await apple_archive.start_import(session, channel=EXPORT, origin="cli")
        await apple_archive.finish_import(session, failed, error="x" * 3000)
    async with sessions() as session:
        done = await session.get(AppleImport, import_id)
        broken = await session.get(AppleImport, failed)
        assert done is not None and broken is not None
        done_status, broken_status = import_as_dict(done), import_as_dict(broken)
    assert done_status["status"] == apple_archive.DONE and done_status["error"] is None
    assert done_status["records_new"] == 5 and done_status["export_date"] == NOW.isoformat()
    assert done_status["summary"] == {"counts": {QUANTITY: 7}}
    assert broken_status["status"] == apple_archive.FAILED
    assert len(broken_status["error"]) == 2000 and broken_status["finished_at"] is not None
