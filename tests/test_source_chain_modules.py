"""Import compatibility and production wiring across the source-chain split."""

import json
import subprocess
import sys
from datetime import timedelta

import pytest
from identity_records import HEART, NEW_NAME, T0, heart, store
from sqlalchemy import event, select

import app.services.identities.source_chain_evidence as source_chain_evidence
import app.services.identities.source_chain_rules as source_chain_rules
from app.core.timeutil import as_utc
from app.db.base import Base
from app.models.identity_tables import RawSourceChain, RawSourceDevice
from app.services.identities import source_chain_repository as repository
from app.services.identities.identity_backfill import identity_backfill
from app.services.identities.identity_options import ChainEvidence, SourceDecision


@pytest.mark.parametrize("first", ["evidence", "model", "rules", "repository", "locks", "storage"])
def test_import_order_has_no_cycle_or_eager_storage_dependency(first):
    module = (
        "app.services.archive.storage"
        if first == "storage"
        else f"app.services.identities.source_chain_{first}"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import {module}; import sys; "
            + (
                "assert 'app.services.identities.source_chains' not in sys.modules; "
                "assert 'app.services.archive.storage' not in sys.modules; "
                if first != "storage"
                else ""
            )
            + "import app.services.identities.source_chains; import app.services.archive.storage; "
            "import app.services.identities.identity; "
            "import app.services.identities.identity_backfill; "
            "import app.services.identities.identity_decisions; "
            "import app.services.identities.identity_verification; "
            "import app.services.identities.identity_reconciler; "
            "import app.services.archive.source_deletions; "
            "import app.cli.main",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


async def snapshot(session):
    return {
        table.name: sorted(
            json.dumps(dict(row), sort_keys=True, default=str)
            for row in (await session.execute(select(table))).mappings()
        )
        for table in Base.metadata.sorted_tables
    }


async def test_legacy_clock_patch_reaches_storage_and_reconciler_sql(sessions, clock):
    await store(sessions, HEART, heart(60))
    first_seen = clock.now
    result = await identity_backfill(sessions, data_types=[HEART.name])
    assert result[HEART.name].error is None
    clock.advance(hours=2)
    await store(sessions, HEART, heart(60, name=NEW_NAME))
    renamed_at = clock.now
    clock.advance(minutes=10)
    result = await identity_backfill(sessions, data_types=[HEART.name], incremental=True)
    assert result[HEART.name].error is None
    async with sessions() as session:
        chains = await repository.load_chains(session, HEART.name)
        assert len(chains.links) == 1
        old, new = next(iter(chains.links))
        assert chains.spans[old].first == chains.spans[old].last == first_seen
        assert chains.spans[new].first == chains.spans[new].last == renamed_at
        assert chains.same_device(old, new)
        link = (await session.scalars(select(RawSourceChain))).one()
        assert as_utc(link.decided_at) == clock.now
        devices = (await session.scalars(select(RawSourceDevice))).all()
        assert len(devices) == 2
        assert all(as_utc(row.decided_at) == clock.now for row in devices)


@pytest.mark.parametrize("commit", [False, True])
async def test_repository_round_trip_shares_the_callers_transaction(sessions, commit):
    old, new = "first", "second"
    now = T0 + timedelta(hours=2)
    spans = {
        old: source_chain_evidence.Span(T0, T0, "band", "old", True, newest=T0),
        new: source_chain_evidence.Span(now, now, "band", "new", True, newest=now),
    }
    async with sessions() as session:
        before = await snapshot(session)
        await repository.write_spans(session, HEART.name, spans, now)
        # A partial re-fetch widens the sighting, preserves completeness and cannot move
        # the physical time of its newest record backwards.
        await repository.record_sightings(
            session, HEART.name, {new: ("band", "new", None, T0)}, now, history_known=False
        )
        chains = await repository.load_chains(session, HEART.name)
        assert chains.spans[new].complete and chains.spans[new].newest == now
        assert source_chain_rules.decide_chains(chains).links == [(old, new)]
        chains.forget(old, new)
        for _ in range(2):
            assert await repository.insert_chain(
                session,
                HEART.name,
                chains,
                old,
                new,
                evidence_record=ChainEvidence(decided_by="reconciler", now=now),
            )
        assert await repository.record_devices(session, HEART.name, chains, now) == {
            old: old,
            new: old,
        }
        assert await repository.record_devices(session, HEART.name, chains, now) == {}
        original = await snapshot(session)
        decision_id = await repository.record_decision(
            session,
            HEART.name,
            chains,
            now=now,
            request=SourceDecision("reject", old, new, None),
        )
        assert decision_id > 0
        assert await repository.record_devices(session, HEART.name, chains, now) == {new: new}

        # The batched state loader still uses one SQL statement, including seen sources
        # and the complete-history flag; it must replay the latest decisions/devices.
        statements = []
        engine = sessions.kw["bind"].sync_engine

        def capture(connection, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", capture)
        try:
            state = await repository.load_state(session, HEART.name, [old, new, "unknown"])
        finally:
            event.remove(engine, "before_cursor_execute", capture)
        assert len(statements) == 1
        assert state.known == {old, new} and state.history_known
        assert state.first_seen == {old: T0, new: now}
        assert state.chains.state(old, new) == "rejected"
        assert state.chains.logged_devices == {old: old, new: new}
        after = await snapshot(session)
        for table in (RawSourceChain, RawSourceDevice):
            assert set(original[table.__tablename__]) <= set(after[table.__tablename__])
        assert len(after[RawSourceChain.__tablename__]) == 1
        assert len(after[RawSourceDevice.__tablename__]) == 3
        if commit:
            await session.commit()
        else:
            await session.rollback()
    async with sessions() as session:
        assert await snapshot(session) == (after if commit else before)


async def test_persisted_premises_evidence_and_refutations_keep_their_meaning(sessions):
    from app.models.identity_tables import (
        IdentityReconcileState,
        RawSourceChainEvent,
        RawSourcePair,
    )

    now = T0 + timedelta(hours=2)
    spans = {
        name: source_chain_evidence.Span(at, at, stable, name, True)
        for name, at, stable in (
            ("a", T0, "band"),
            ("b", now, "band"),
            ("c", T0, "watch"),
            ("d", now, "watch"),
        )
    }
    async with sessions.begin() as session:
        await repository.write_spans(session, HEART.name, spans, now)
        chains = await repository.load_chains(session, HEART.name)
        chains.set_premise("c", "d", frozenset({("a", "b")}))
        for old, new in (("a", "b"), ("c", "d")):
            assert await repository.insert_chain(
                session,
                HEART.name,
                chains,
                old,
                new,
                evidence_record=ChainEvidence(decided_by="reconciler", now=now),
            )
    async with sessions.begin() as session:
        chains = await repository.load_chains(session, HEART.name)
        assert chains.premises == {("c", "d"): frozenset({("a", "b")})}
        assert chains.live("c", "d")
        await repository.record_decision(
            session,
            HEART.name,
            chains,
            now=now,
            request=SourceDecision("reject", "a", "b", None),
        )
        assert chains.state("c", "d") == "lapsed"
        await repository.record_decision(
            session,
            HEART.name,
            chains,
            now=now,
            request=SourceDecision("confirm", "c", "d", None),
        )
        assert chains.live("c", "d")
        # Two keys from one fetch do not establish distinct devices. A third key,
        # newer than the first fetch and returned in a later one, crosses the threshold.
        early = [("d", "c", f"v2:20260101T00000{index}:sample", T0) for index in (1, 2)]
        late = [("c", "d", f"v2:{now:%Y%m%dT%H%M%S}:sample", now)]
        assert await repository.record_evidence(session, HEART.name, chains, early) == 2
        assert not chains.evidence_distinct("c", "d")
        assert await repository.record_evidence(session, HEART.name, chains, late) == 1
        assert chains.evidence_distinct("c", "d")
        assert await repository.record_evidence(session, HEART.name, chains, early + late) == 0
        # Refutation must wait until every shared key has been counted.
        assert await repository.record_refutations(session, HEART.name, chains, now) == []
        chains.update_agreement({("c", "d"): (0, 3, True)})
        session.add(
            IdentityReconcileState(
                data_type=HEART.name,
                runs=0,
                pair_agreement=source_chain_evidence.dump_agreement(chains.agreement),
            )
        )
        assert await repository.record_refutations(session, HEART.name, chains, now) == [("c", "d")]
        assert await repository.record_refutations(session, HEART.name, chains, now) == []
    async with sessions() as session:
        chains = await repository.load_chains(session, HEART.name)
        assert chains.state("a", "b") == "rejected"
        assert chains.state("c", "d") == "refuted"
        assert not chains.same_device("c", "d")
        assert chains.agreement[("c", "d")] == (0, 3, True)
        assert len((await session.scalars(select(RawSourceChain))).all()) == 2
        assert len((await session.scalars(select(RawSourcePair))).all()) == 3
        assert len((await session.scalars(select(RawSourceChainEvent))).all()) == 1
