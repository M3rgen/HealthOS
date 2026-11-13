"""Rename sightings and owner review remain visible through archive and CLI paths."""

from __future__ import annotations

import asyncio
import json

from identity_records import (
    HEART,
    T0,
    heart,
    store,
)
from sqlalchemy import event
from support.identity import MINUTE
from test_identity_rename_sightings import HOUR, NEW, OLD, _rename_archive_db
from typer.testing import CliRunner


async def test_a_page_records_its_sightings_last_with_few_statements(sessions, clock) -> None:
    """Contention and cost: the sightings row (the one row every writer of a data type
    shares) is written last in the page; an unchanged page needs few statements."""
    records = [heart(60 + minute, at=T0 + minute * MINUTE) for minute in range(5)]
    await store(sessions, HEART, *records)
    statements: list[str] = []
    engine = sessions.kw["bind"].sync_engine

    def seen(_connection, _cursor, statement, *_args) -> None:
        statements.append(statement.split()[0] + " " + statement.split()[2])

    event.listen(engine, "before_cursor_execute", seen)
    try:
        clock.advance(hours=1)
        await store(sessions, HEART, *records)
    finally:
        event.remove(engine, "before_cursor_execute", seen)
    assert statements[-1] == "INSERT raw_source_sightings"
    assert len(statements) <= 7, statements


def test_status_lists_chains_awaiting_review(tmp_path, monkeypatch) -> None:
    """B6: healthos status lists every chain the reconciler decided that the owner
    has neither confirmed nor rejected."""

    from app.cli import main as cli

    engine, factory = _rename_archive_db(tmp_path, 8 * HOUR)
    monkeypatch.setattr(cli, "SessionLocal", factory)
    try:
        assert CliRunner().invoke(cli.app, ["identity-backfill"]).exit_code == 0
        status = json.loads(CliRunner().invoke(cli.app, ["status"]).stdout)
        [chain] = status["identity_review_suggested"]
        assert chain["fingerprints"] == [OLD[:12], NEW[:12]] and chain["decided_by"] == "backfill"
        decided = CliRunner().invoke(
            cli.app,
            [
                "identity-decide",
                "confirm",
                "--data-type",
                "heart-rate",
                "--from",
                OLD[:12],
                "--to",
                NEW[:12],
            ],
        )
        assert decided.exit_code == 0, decided.output
        assert (
            json.loads(CliRunner().invoke(cli.app, ["status"]).stdout)["identity_review_suggested"]
            == []
        )
        refused = CliRunner().invoke(
            cli.app,
            [
                "identity-decide",
                "reject",
                "--data-type",
                "heart-rate",
                "--from",
                NEW[:12],
                "--to",
                OLD[:12],
            ],
        )
        assert refused.exit_code == 1 and "no chain" in refused.output
    finally:
        asyncio.run(engine.dispose())
