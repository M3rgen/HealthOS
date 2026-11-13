"""Deletion decisions for synthetic growing intervals and their current holders."""

from datetime import timedelta

import pytest
from identity_records import SEDENTARY, T0, fitbit, interval, mark, raw_rows
from sqlalchemy import select

from app.core.timeutil import as_utc
from app.models.identity_tables import RawHealthDataSupersession
from app.models.state import RawHealthData
from app.parsers.common import payload_hash
from app.parsers.identity import source_fingerprint


def period(minutes):
    return {"dataSource": fitbit(), "sedentaryPeriod": {"interval": interval(T0, minutes)}}


@pytest.mark.parametrize("holder_state", ["live", "deleted", "quarantined", "unlinked"])
@pytest.mark.parametrize("minutes", [10, 15, 45])
async def test_only_live_holder_keeps_unseen_interval(sessions, holder_state, minutes):
    since = T0 + timedelta(hours=2)
    old_payload, returned_payload = period(15), period(minutes)
    old, returned = [
        RawHealthData(
            google_data_point_id=f"synthetic:presence:{label}",
            data_type=SEDENTARY.name,
            source_start_time=T0,
            source_end_time=T0 + timedelta(minutes=length),
            raw_json=payload,
            payload_hash=payload_hash(payload),
            first_seen_at=T0,
            last_seen_at=seen,
            source_deleted_at=since if label == "returned" and holder_state == "deleted" else None,
            is_quarantined=label == "returned" and holder_state == "quarantined",
        )
        for label, length, payload, seen in [
            ("old", 15, old_payload, T0),
            ("returned", minutes, returned_payload, since),
        ]
    ]
    async with sessions.begin() as session:
        session.add_all([old, returned])
        await session.flush()
        if holder_state != "unlinked":
            fingerprint = source_fingerprint(fitbit())
            session.add(
                RawHealthDataSupersession(
                    old_raw_id=old.id,
                    new_raw_id=returned.id,
                    data_type=SEDENTARY.name,
                    old_google_data_point_id=old.google_data_point_id,
                    new_google_data_point_id=returned.google_data_point_id,
                    old_fingerprint=fingerprint,
                    new_fingerprint=fingerprint,
                    reason="growing-interval",
                    same_reading=True,
                    # The holder changed since linking: bypass the unchanged-holder shortcut.
                    new_payload_hash=payload_hash(period(30)),
                )
            )

    kept = holder_state == "live" and minutes >= 15
    assert await mark(sessions, SEDENTARY, T0, T0 + timedelta(hours=1), since) == int(not kept)
    rows = {row.id: row for row in await raw_rows(sessions, SEDENTARY)}
    assert len(rows) == 2
    assert (rows[old.id].source_deleted_at is None) == kept
    assert rows[old.id].raw_json == old_payload
    assert rows[returned.id].raw_json == returned_payload
    deleted_at = rows[returned.id].source_deleted_at
    assert (as_utc(deleted_at) if deleted_at else None) == returned.source_deleted_at
    assert rows[returned.id].is_quarantined == returned.is_quarantined
    async with sessions() as session:
        links = (await session.scalars(select(RawHealthDataSupersession))).all()
    assert len(links) == int(holder_state != "unlinked")
