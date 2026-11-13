"""Synthetic input fixtures and a standalone real-database latency measurement."""

import asyncio
import json
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID

from derived_records import DAY, T0
from sqlalchemy import event, insert
from sqlalchemy.dialects.postgresql.asyncpg import dialect
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401 - complete synthetic SQLite schema
from app.db.base import Base
from app.models.state import RawHealthData
from app.services.interpretation import interpretation_inputs as inputs
from app.services.shared.query_ranges import CivilDateRange


class LoopTurns:
    """How much work the event loop did between two of its turns; no clock is involved.

    A slow machine runs the same code with the same yields, so the result does not depend on the
    host's load, and a code path that stops yielding shows up as work piling between two turns.
    """

    def __init__(self) -> None:
        self.turns = 0
        self.pending = 0
        self.worst = 0

    def work(self, units: int = 1) -> None:
        self.pending += units
        self.worst = max(self.worst, self.pending)

    def turn(self) -> None:
        self.turns += 1
        self.pending = 0


class ThreadBoundRow(SimpleNamespace):
    """Fail on even loaded ORM-like attribute reads from a worker thread."""

    meter: LoopTurns | None = None

    def __getattribute__(self, name):
        assert threading.get_ident() == object.__getattribute__(self, "_owner")
        if name == "raw_json" and ThreadBoundRow.meter is not None:
            ThreadBoundRow.meter.work()
        return super().__getattribute__(name)


def raw_rows(count):
    owner = threading.get_ident()
    return [
        ThreadBoundRow(
            _owner=owner,
            id=UUID(int=i + 1),
            data_type="steps",
            raw_json={
                "dataSource": {"device": {"displayName": f"sentetik-{i}"}},
                "steps": {"count": i, "description": "sentetik ölçüm" * 8},
            },
            source_start_time=T0,
            source_end_time=T0,
            source_timezone="UTC",
            source_deleted_at=None,
            is_quarantined=False,
        )
        for i in range(count)
    ]


@asynccontextmanager
async def counted_loop_turns():
    """A task that runs once per loop turn; it counts every turn the measured code gives back."""
    meter = LoopTurns()
    done = asyncio.Event()

    async def ticker():
        while not done.is_set():
            meter.turn()
            await asyncio.sleep(0)

    monitor = asyncio.create_task(ticker())
    await asyncio.sleep(0)
    try:
        yield meter
    finally:
        done.set()
        await monitor


def record_largest_encoding(sizes: list[int]) -> None:
    """Note the largest single JSON encoding: one huge ``json.dumps`` holds the GIL for its length.

    This process ends with the sample, so the patch is not undone.
    """
    original = json.JSONEncoder.encode

    def encode(self, value):
        text = original(self, value)
        sizes.append(len(text))
        return text

    json.JSONEncoder.encode = encode


def synthetic_session(rows):
    owner = threading.get_ident()
    eligible = [row.id for row in rows if row.id.int % 7]
    results = iter([rows, eligible, [], [], [], []])
    session = Mock()
    session.get_bind.return_value.dialect = dialect()

    async def scalars(statement):
        assert threading.get_ident() == owner
        await asyncio.sleep(0)
        return next(results)

    async def execute(statement):
        assert threading.get_ident() == owner
        await asyncio.sleep(0)
        # The SQL size check may undercount canonical UTF-8 bytes; it is only a preflight.
        return Mock(one=lambda: (len(rows), 0), all=lambda: [])

    async def stream_scalars(statement):
        rows = await scalars(statement)

        async def partitions(size):
            for offset in range(0, len(rows), size):
                yield rows[offset : offset + size]

        return Mock(partitions=partitions, close=AsyncMock())

    session.stream_scalars = stream_scalars
    session.scalars, session.execute = scalars, execute
    return session, eligible


async def synthetic_sample():
    """44,000 inputs over a mocked session; only worker threads may do the CPU work.

    The rows read on the session's thread between two loop turns and the size of each JSON
    encoding are counted, not timed: both are the same on an idle laptop and a busy CI host.
    """
    rows = raw_rows(44_000)
    session, eligible = synthetic_session(rows)
    owner = threading.get_ident()
    workers = []
    for name in ("_raw_keys", "_input_fingerprint"):
        original = getattr(inputs, name)

        def checked(*args, original=original, **kwargs):
            assert threading.get_ident() != owner
            workers.append(threading.get_ident())
            return original(*args, **kwargs)

        setattr(inputs, name, checked)  # This process ends with the sample.
    encodings: list[int] = []
    record_largest_encoding(encodings)
    async with counted_loop_turns() as meter:
        ThreadBoundRow.meter = meter
        loaded = await inputs.load_inputs(session, {"steps"}, dates=CivilDateRange(DAY, DAY, "UTC"))
        ThreadBoundRow.meter = None
    assert [row.id for row in loaded.rows] == eligible
    assert loaded.measurements == loaded.workouts == [] and loaded.devices == {}
    session.commit.assert_not_called()
    session.rollback.assert_not_called()
    return {
        "worst_rows_between_turns": meter.worst,
        "turns": meter.turns,
        "workers": len(workers),
        "largest_encoding": max(encodings),
        "fingerprint": loaded.fingerprint,
    }


async def database_sample():
    """Count ORM rows hydrated between two loop turns on a fresh synthetic archive."""
    from support.sqlite_database import install

    install()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    values = [
        {key: value for key, value in vars(row).items() if key != "_owner"}
        | {
            "google_data_point_id": f"synthetic/latency/{i}",
            "payload_hash": "a" * 64,
            "first_seen_at": T0,
            "last_seen_at": T0,
        }
        for i, row in enumerate(raw_rows(44_000))
    ]
    async with sessions.begin() as session:
        await session.execute(insert(RawHealthData), values)
    owner = threading.get_ident()
    statements = []

    def check_session_thread(conn, cursor, statement, parameters, context, many):
        assert threading.get_ident() == owner
        statements.append(statement)

    hydrated = {"meter": None}

    def count_hydrated(_target, _context):
        if hydrated["meter"] is not None:
            hydrated["meter"].work()

    event.listen(RawHealthData, "load", count_hydrated)
    async with sessions() as session:
        session_engine = session.get_bind()
        event.listen(session_engine, "before_cursor_execute", check_session_thread)
        try:
            async with counted_loop_turns() as meter:
                hydrated["meter"] = meter
                loaded = await inputs.load_inputs(
                    session, {"steps"}, dates=CivilDateRange(DAY, DAY, "UTC")
                )
                hydrated["meter"] = None
        finally:
            event.remove(session_engine, "before_cursor_execute", check_session_thread)
        assert session.in_transaction()
        assert len(loaded.rows) == 44_000 and len(loaded.fingerprint) == 64
    assert statements and all(statement.startswith("SELECT") for statement in statements)
    report = {
        "worst_rows_between_turns": meter.worst,
        "turns": meter.turns,
        "rows": len(loaded.rows),
        "fingerprint": loaded.fingerprint,
    }
    await engine.dispose()
    return report
