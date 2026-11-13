"""Revived and quarantined holders retain the reconciler's counting rules."""

from __future__ import annotations

from identity_records import (
    HEART,
    T0,
    counted_rows,
    heart,
    legacy,
    store,
)
from sqlalchemy import select, update
from test_identity_agreement_and_flapping import (
    A1,
    A2,
    HOUR,
    MINUTE,
    nightly,
    problems,
    renamed_with_history,
)

from app.models.identity_tables import RawHealthDataSupersession
from app.models.state import RawHealthData
from app.services.identities.identity_backfill import identity_backfill


async def test_a_revived_row_of_a_decided_device_is_counted_once_at_once(sessions, clock):
    """Review 6b M5: a marked row whose record came back with the reading the device's other
    name shows was counted twice until the next reconciler run (240 records)."""
    keys = [T0 + index * MINUTE for index in range(3)]
    for at in keys:
        await legacy(sessions, HEART, heart(59, at=at, name=A1), at, deleted=T0 + HOUR)
    await identity_backfill(sessions)
    clock.now = T0 + 5 * HOUR
    await store(sessions, HEART, *(heart(60, at=at, name=A2) for at in keys))
    clock.now = T0 + 6 * HOUR
    await nightly(sessions)
    assert len(await counted_rows(sessions)) == 3
    clock.advance(hours=1)
    await store(sessions, HEART, *(heart(60, at=at, name=A1) for at in keys))
    assert len(await counted_rows(sessions)) == 3
    assert (await problems(sessions))[0] == []


async def stored_row(sessions, record, google_id, seen, deleted=None):
    from app.parsers.common import payload_hash, temporal

    start, end, zone = temporal(HEART, record)
    row = RawHealthData(
        google_data_point_id=google_id,
        data_type="heart-rate",
        source_start_time=start,
        source_end_time=end,
        source_timezone=zone,
        raw_json=record,
        payload_hash=payload_hash(record),
        first_seen_at=seen,
        last_seen_at=seen,
        source_deleted_at=deleted,
    )
    async with sessions.begin() as session:
        session.add(row)
    return row


async def link(sessions, old, new, reason="display-name-change"):
    from app.parsers.identity import record_identity

    async with sessions.begin() as session:
        session.add(
            RawHealthDataSupersession(
                old_raw_id=old.id,
                new_raw_id=new.id,
                data_type="heart-rate",
                old_google_data_point_id=old.google_data_point_id,
                new_google_data_point_id=new.google_data_point_id,
                reason=reason,
                old_fingerprint=record_identity(HEART, old.raw_json).source,
                new_fingerprint=record_identity(HEART, new.raw_json).source,
                same_reading=True,
                new_payload_hash=new.payload_hash,
            )
        )


async def test_a_hidden_root_beside_a_fitting_row_never_sticks_a_page(sessions, clock) -> None:
    """Review 6a minor (identity.py:588 vs :758): spares were probed only when the v2s row did
    not fit, while the row written onto was the first root in order; a v1 row first in order
    that did not fit (hidden behind another value) beside a fitting v2s row and an existing
    spare made the page insert that spare's id again (a RuntimeError on every sync). Any own
    row that fits is written onto."""
    from app.parsers.common import source_id
    from app.parsers.identity import new_record_id, record_identity, spare_record_id

    await renamed_with_history(sessions, clock, [T0 + HOUR])  # A1 -> A2 decided
    mine = heart(60, name=A1)
    identity = record_identity(HEART, mine)
    assert identity is not None
    other61, other60 = heart(61, name=A2), heart(60, name=A2)
    shows61 = await stored_row(
        sessions, other61, new_record_id(record_identity(HEART, other61).specific_key), T0
    )
    shows60 = await stored_row(
        sessions, other60, spare_record_id(record_identity(HEART, other60), 1), T0
    )
    v1 = await stored_row(sessions, heart(59, name=A1), source_id(HEART, mine), T0 + 2 * HOUR)
    v2s = await stored_row(sessions, heart(58, name=A1), new_record_id(identity.specific_key), T0)
    spare = await stored_row(sessions, heart(57, name=A1), spare_record_id(identity, 1), T0)
    await link(sessions, v1, shows61)
    await link(sessions, v2s, shows60)
    await link(sessions, spare, shows61)
    clock.now = T0 + 9 * HOUR
    stats = await store(sessions, HEART, mine)
    assert (stats.inserted, stats.updated) == (0, 1)  # onto the fitting v2s row
    assert any(row.id == shows60.id for row in await counted_rows(sessions))


async def test_the_reconciler_ignores_a_quarantined_holder(sessions, clock) -> None:
    """Review 6a minor: the reconciler took a row held by a quarantined row for hidden (unlike
    readers), so it was never linked to the live row of its device."""
    keys = [T0]
    await renamed_with_history(sessions, clock, keys)
    async with sessions() as session:
        holder = await session.scalar(select(RawHealthDataSupersession.new_raw_id))
    async with sessions.begin() as session:
        await session.execute(
            update(RawHealthData).where(RawHealthData.id == holder).values(is_quarantined=True)
        )
    report = (await identity_backfill(sessions))["heart-rate"]
    assert report.already_superseded == 0  # readers count it again: so does the reconciler
