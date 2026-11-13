"""Only a live holder and a link still in force can spare an unseen archive row."""

from datetime import timedelta

import pytest
from identity_records import HEART, T0, heart, legacy, mark, raw_rows

from app.models.identity_tables import RawHealthDataSupersession
from app.models.state import RawHealthData


@pytest.mark.parametrize("holder_state,expected", [("live", 0), ("deleted", 1), ("unlinked", 1)])
async def test_complete_fetch_spares_only_live_in_force_holders(sessions, holder_state, expected):
    since = T0 + timedelta(days=1)
    # Named historical duplicates do not also take the nameless-identity presence path.
    old_payload = {**heart(60), "name": "users/synthetic/dataTypes/heart-rate/dataPoints/old"}
    new_payload = {**heart(60), "name": "users/synthetic/dataTypes/heart-rate/dataPoints/new"}
    old = await legacy(sessions, HEART, old_payload, T0)
    new = await legacy(
        sessions,
        HEART,
        new_payload,
        since,
        deleted=since if holder_state == "deleted" else None,
    )
    async with sessions.begin() as session:
        holder = await session.get(RawHealthData, new)
        session.add(
            RawHealthDataSupersession(
                old_raw_id=old,
                new_raw_id=new,
                data_type=HEART.name,
                old_google_data_point_id=old_payload["name"],
                new_google_data_point_id=new_payload["name"],
                reason="duplicate",
                old_fingerprint="a" * 64,
                new_fingerprint=("b" if holder_state == "unlinked" else "a") * 64,
                same_reading=True,
                new_payload_hash=holder.payload_hash,
            )
        )
    assert await mark(sessions, HEART, T0, T0 + timedelta(hours=1), since) == expected
    rows = {row.id: row for row in await raw_rows(sessions, HEART)}
    assert len(rows) == 2  # the archive and both payloads survive
    assert rows[old].raw_json == old_payload and rows[new].raw_json == new_payload
    assert (rows[old].source_deleted_at is not None) == bool(expected)
    assert (rows[new].source_deleted_at is not None) == (holder_state == "deleted")


@pytest.mark.parametrize("same_reading,expected", [(True, 0), (False, 1)])
async def test_a_holder_that_read_something_else_does_not_spare_the_row(
    sessions, same_reading, expected
):
    """A link in force whose two records disagreed on the value says the holder is the current
    record of another reading: the unseen row's own absence decides, not the holder's presence."""
    since = T0 + timedelta(days=1)
    old_payload = {**heart(60), "name": "users/synthetic/dataTypes/heart-rate/dataPoints/old"}
    new_payload = {**heart(61), "name": "users/synthetic/dataTypes/heart-rate/dataPoints/new"}
    old = await legacy(sessions, HEART, old_payload, T0)
    new = await legacy(sessions, HEART, new_payload, since)
    async with sessions.begin() as session:
        holder = await session.get(RawHealthData, new)
        session.add(
            RawHealthDataSupersession(
                old_raw_id=old,
                new_raw_id=new,
                data_type=HEART.name,
                old_google_data_point_id=old_payload["name"],
                new_google_data_point_id=new_payload["name"],
                reason="correction",
                old_fingerprint="a" * 64,
                new_fingerprint="a" * 64,
                same_reading=same_reading,
                new_payload_hash=holder.payload_hash,
            )
        )
    assert await mark(sessions, HEART, T0, T0 + timedelta(hours=1), since) == expected
    rows = {row.id: row for row in await raw_rows(sessions, HEART)}
    assert rows[old].raw_json == old_payload and rows[new].raw_json == new_payload
    assert (rows[old].source_deleted_at is not None) == bool(expected)
    assert rows[new].source_deleted_at is None
