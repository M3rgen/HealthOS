"""Window resolution keeps all undecided evidence, on SQLite as well as PostgreSQL."""

from datetime import timedelta
from uuid import UUID

import pytest
from identity_records import T0
from sqlalchemy import select

from app.core.timeutil import as_utc
from app.models.identity_tables import IdentityDeferredDeletion as Deferral
from app.models.state import RawHealthData
from app.parsers.common import payload_hash
from app.services.archive import source_deletions as deletions

HOUR = timedelta(hours=1)


@pytest.mark.parametrize(
    "data_type,column", [("heart-rate", "source_start_time"), ("sleep", "source_end_time")]
)
async def test_only_decided_rows_of_the_half_open_window_are_resolved(
    sessions, monkeypatch, data_type, column
):
    monkeypatch.setattr(deletions, "CHUNK", 2)
    times = {
        "lower": T0,
        "inside": T0 + timedelta(minutes=1),
        "last": T0 + HOUR - timedelta(microseconds=1),
        "before": T0 - timedelta(microseconds=1),
        "upper": T0 + HOUR,
        "unknown": None,
        "again": T0,
        "skipped": T0,
        "foreign_deferral": T0,
        "foreign_raw": T0,
        "resolved": T0,
    }
    ids = {name: UUID(int=index) for index, name in enumerate(times, 1)}
    previous, now = T0 + 2 * HOUR, T0 + 3 * HOUR
    async with sessions.begin() as session:
        for name, value in times.items():
            payload = {"synthetic": name}
            session.add(
                RawHealthData(
                    id=ids[name],
                    google_data_point_id=f"synthetic:{name}",
                    data_type="steps" if name == "foreign_raw" else data_type,
                    **{column: value},
                    raw_json=payload,
                    payload_hash=payload_hash(payload),
                    first_seen_at=T0,
                    last_seen_at=T0,
                )
            )
        await session.flush()
        session.add_all(
            Deferral(
                raw_id=ids[name],
                data_type="steps" if name == "foreign_deferral" else data_type,
                fingerprint="a" * 64,
                returned_by="b" * 64,
                first_deferred_at=previous,
                last_deferred_at=previous,
                resolved_at=previous if name == "resolved" else None,
            )
            for name in times
        )
    async with sessions.begin() as session:
        await deletions._note_deferred(
            session,
            data_type,
            {ids["again"]: ("a" * 64, "c" * 64)},
            {ids["skipped"]},
            scope=deletions.DeletionWindow(
                column=getattr(RawHealthData, column), window=(T0, T0 + HOUR), now=now
            ),
        )
    async with sessions() as session:
        found = {row.raw_id: row for row in await session.scalars(select(Deferral))}
        raw = list(await session.scalars(select(RawHealthData)))
    assert len(raw) == len(found) == len(times)
    assert {row.id: row.raw_json for row in raw} == {
        raw_id: {"synthetic": name} for name, raw_id in ids.items()
    }
    assert all(row.source_deleted_at is None for row in raw)
    for name, raw_id in ids.items():
        entry = found[raw_id]
        expected = (
            now
            if name in {"lower", "inside", "last"}
            else (previous if name == "resolved" else None)
        )
        assert (as_utc(entry.resolved_at) if entry.resolved_at else None) == expected, name
        assert as_utc(entry.first_deferred_at) == previous
        assert as_utc(entry.last_deferred_at) == (now if name == "again" else previous)
        assert entry.returned_by == ("c" if name == "again" else "b") * 64
