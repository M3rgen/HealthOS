"""Full canonical evidence stays equivalent while raw documents leave each batch."""

import asyncio
import gc
import hashlib
import json
import tracemalloc
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from derived_records import (
    DAY,
    PHONE,
    T0,
    manual,
    sleep,
    snapshot,
    steps,
    store,
    summary,
)
from sqlalchemy import event, insert, select
from sqlalchemy.ext.asyncio import AsyncResult
from support.postgres_reads import rows_in_flight, socket_buffers

from app.models.health import Steps
from app.models.manual import ManualMeasurement
from app.models.state import RawHealthData
from app.services.interpretation import canonical_daily as reader

SUMMARY_KINDS = tuple(reader.DAILY_FIELDS)
DAYS = [
    (DAY, "UTC"),
    (date(2026, 3, 29), "Europe/Brussels"),
    (date(2026, 10, 25), "Europe/Brussels"),
]


def records(day, zone):
    first, stop = reader.bounds(day, zone)

    def stage(label, a, b):
        return {
            "type": label,
            "startTime": (first + timedelta(hours=a)).isoformat(),
            "endTime": (first + timedelta(hours=b)).isoformat(),
        }

    return {
        SUMMARY_KINDS[0]: [
            summary(SUMMARY_KINDS[0], "conflict-a", day, beatsPerMinute=60),
            summary(SUMMARY_KINDS[0], "conflict-b", day, beatsPerMinute=70),
            summary(SUMMARY_KINDS[0], "zero", day, PHONE, beatsPerMinute=0),
            summary(
                SUMMARY_KINDS[0],
                "method",
                day,
                beatsPerMinute=80,
                dailyRestingHeartRateMetadata={"method": "synthetic"},
            ),
        ],
        SUMMARY_KINDS[1]: [
            summary(
                SUMMARY_KINDS[1],
                "aliases",
                day,
                rmssd=11,
                rootMeanSquareOfSuccessiveDifferencesMilliseconds=22,
                averageHeartRateVariabilityMilliseconds=None,
            ),
            summary(SUMMARY_KINDS[1], "unknown", day, {}, rmssd=0),
        ],
        SUMMARY_KINDS[2]: [summary(SUMMARY_KINDS[2], day=day, averagePercentage=0)],
        "steps": [
            steps("fix", 120, first - timedelta(hours=1), first + timedelta(hours=1)),
            steps("later", 300, first + timedelta(hours=2), stop),
            steps("phone", 0, first, stop, PHONE),
            steps("raw-only", -1, first, stop, {}),
            *(
                steps(name, 900, first, stop)
                for name in ("deleted", "raw-quarantine", "quarantine")
            ),
        ],
        "sleep": [
            sleep(
                "fix",
                first - timedelta(hours=2),
                first + timedelta(hours=4),
                summary={"minutesAsleep": 120},
                stages=[stage("deep", 0, 2), stage("awake", 1, 3)],
            ),
            # Same bounds and totals, different clocks: stable session ordering matters.
            sleep("tie-b", first, first + timedelta(hours=3), stages=[stage("light", 1, 2)]),
            sleep("tie-a", first, first + timedelta(hours=3), stages=[stage("light", 0, 1)]),
            sleep("phone", first, first + timedelta(hours=3), PHONE, summary={"minutesAsleep": 0}),
            sleep("raw-only", first, first + timedelta(hours=2), {}, duration="-1s"),
            sleep("midnight", first - timedelta(hours=2), first, summary={"minutesAsleep": 30}),
            sleep("exclusive", stop - timedelta(hours=2), stop),
        ],
    }


async def prepare(sessions, day, zone, reverse):
    for kind, rows in records(day, zone).items():
        await store(sessions, kind, *(reversed(rows) if reverse else rows))
    await manual(sessions, 70, at=reader.bounds(day, zone)[0])
    async with sessions.begin() as session:
        rows = (
            await session.scalars(select(RawHealthData).where(RawHealthData.data_type == "steps"))
        ).all()
        for row in rows:
            name = row.google_data_point_id.rsplit("/", 1)[-1]
            if name == "deleted":
                row.source_deleted_at = T0
            elif name == "raw-quarantine":
                row.is_quarantined = True
            elif name == "quarantine":
                normalized = await session.scalar(
                    select(Steps).where(Steps.google_data_point_id == row.google_data_point_id)
                )
                normalized.is_quarantined = True


# SHA-256 of the complete JSON output before streaming (6e6e203).
# Normalize the manual UUID and method tag; these fixtures contain no negative summaries.
GOLDEN = {
    ("2026-09-29", "initial"): "c9655e07f5b5361b44637d702574e28a8136d44cb4995198b4f1380c900366c2",
    ("2026-09-29", "invalid"): "52341ab41a166e178a618a07ae821558c9205e8176b8842aea46f2bd3f01fbbc",
    ("2026-09-29", "recovered"): "722d63ffe914a535280f74e847d45090b5feadd27aea9134c44b0cd5d6609fe5",
    ("2026-03-29", "initial"): "be83ce29e122dc4621ff7bd49b423f320d2241002e4f0f228758cf82a6a70eed",
    ("2026-03-29", "invalid"): "c01fa2ce23a08d2ea93829545ef08b06d8f7e2c4b1a2e7b04268009c59e0d4af",
    ("2026-03-29", "recovered"): "a76fa0120f6bdf2cd230c4ea74d7b577f60cac29bc1caee37af3dbfbef88fa0f",
    ("2026-10-25", "initial"): "2d21116914188ef1f2ff53938a3d6d0d5246e0cba47316e41f84e0ddb7ccd008",
    ("2026-10-25", "invalid"): "26781b5aaa987993a2a15b9d1accb67b3434ff8a363cde9437b94762bf9aac2e",
    ("2026-10-25", "recovered"): "b53e5ec9441457139dd77b73023e4ac945fbe7fb12f19013093c4c2a4e009c62",
}


@pytest.mark.parametrize("batch,reverse", [(1, False), (3, True), (100, False)])
@pytest.mark.parametrize("day,zone", DAYS)
async def test_complete_evidence_matches_before_streaming(
    sessions, monkeypatch, batch, reverse, day, zone
):
    monkeypatch.setattr(reader, "_RAW_BATCH", batch)
    await prepare(sessions, day, zone, reverse)
    first, stop = reader.bounds(day, zone)
    for phase in ("initial", "invalid", "recovered"):
        if phase != "initial":
            await store(
                sessions,
                "steps",
                steps(
                    "fix",
                    -1 if phase == "invalid" else 0,
                    first - timedelta(hours=1),
                    first + timedelta(hours=1),
                ),
            )
            await store(
                sessions,
                "sleep",
                sleep(
                    "fix",
                    first - timedelta(hours=1),
                    first + timedelta(hours=4),
                    duration="-1s" if phase == "invalid" else "18000s",
                    summary={"minutesAsleep": 0},
                ),
            )
            await store(
                sessions,
                SUMMARY_KINDS[2],
                summary(
                    SUMMARY_KINDS[2], day=day, averagePercentage="bad" if phase == "invalid" else 97
                ),
            )
        async with sessions.begin() as session:
            before = await snapshot(session)
            result = await reader.canonical_day(session, day, zone)
            assert await snapshot(session) == before
            assert result["day_seconds"] == (stop - first).total_seconds()
            assert result["method_version"] == "source-daily/12"
            for profile in result["manual_profile"]:
                for observation in profile["observations"]:
                    observation["record"] = "synthetic-manual"
            serialized = json.dumps(result, sort_keys=True, ensure_ascii=False).replace(
                '"source-daily/12"', '"source-daily/8"'
            )
            digest = hashlib.sha256(serialized.encode()).hexdigest()
            assert digest == GOLDEN[str(day), phase], serialized


@pytest.mark.parametrize("kind", [SUMMARY_KINDS[0], "steps", "sleep", "manual"])
@pytest.mark.parametrize("failure", [None, "parse", "evaluate", "fetch", "cancel"])
async def test_cursor_closes_without_taking_session_or_transaction(
    sessions, monkeypatch, kind, failure
):
    if kind == "manual":
        for value in (70, 80, 90):
            await manual(sessions, value)
        # Failure must leave actual archive history intact, too.
        await store(sessions, "steps", steps())
        await store(sessions, "steps", steps(count=0))
    else:
        await store(sessions, kind, *records(DAY, "UTC")[kind][:3])
    monkeypatch.setattr(reader, "_RAW_BATCH", 1)
    monkeypatch.setattr(reader, "_MANUAL_BATCH", 1)
    async with sessions.begin() as session:
        before = await snapshot(session)
        transaction = session.get_transaction()
        stream = session.stream
        target = {SUMMARY_KINDS[0]: 0, "steps": 1, "sleep": 2, "manual": 0}[kind]
        parse_name = {
            SUMMARY_KINDS[0]: "payload_for",
            "steps": "step_window",
            "sleep": "sleep_window",
            "manual": "as_utc",
        }[kind]
        parse = getattr(reader, parse_name)
        opened, seen = [], 0
        fetched = {}

        async def tracked(statement, **kwargs):
            assert kwargs["execution_options"] == {"yield_per": 1}
            expected = {
                "google_data_point_id",
                "raw_json",
                "source_start_time",
                "source_end_time",
                "source_timezone",
            }
            if kind == "manual" or len(opened) >= 3:
                expected = {"id", "value", "measured_at"}
            elif not opened:
                expected.add("data_type")
            assert set(statement.selected_columns.keys()) == expected
            result = await stream(statement, **kwargs)
            opened.append(result)
            fetched[id(result)] = 0
            monkeypatch.setattr(result, "close", AsyncMock(wraps=result.close))
            return result

        next_row = AsyncResult.__anext__

        async def interrupted_next(result):
            if len(opened) == target + 1 and result is opened[target] and fetched[id(result)] == 2:
                if failure == "cancel":
                    asyncio.current_task().cancel()
                    await asyncio.sleep(0)  # deliver real cancellation at a fetch boundary
                if failure == "fetch":
                    raise ValueError("synthetic fetch failure")
            row = await next_row(result)
            fetched[id(result)] += 1
            return row

        def interrupted_parse(*args, **kwargs):
            nonlocal seen
            seen += 1
            if seen == 2 and failure == "parse":
                raise ValueError("synthetic parse failure")
            return parse(*args, **kwargs)

        owner = reader.SleepEvidenceAccumulator if kind == "sleep" else reader
        evaluate_name = (
            "float" if kind == "manual" else "add" if kind == "sleep" else "_observation"
        )
        evaluate = getattr(owner, evaluate_name, float)

        def interrupted_evaluate(*args, **kwargs):
            if seen == 2 and failure == "evaluate":
                raise ValueError("synthetic evaluation failure")
            return evaluate(*args, **kwargs)

        monkeypatch.setattr(session, "stream", tracked)
        monkeypatch.setattr(AsyncResult, "__anext__", interrupted_next)
        monkeypatch.setattr(reader, parse_name, interrupted_parse)
        monkeypatch.setattr(owner, evaluate_name, interrupted_evaluate, raising=False)
        task = asyncio.create_task(
            reader._manual_profile(session, reader.bounds(DAY, "UTC")[1])
            if kind == "manual"
            else reader.canonical_day(session, DAY, "UTC")
        )
        if failure is None:
            result = await task
            assert result if kind == "manual" else result["streams"]
        else:
            with pytest.raises(asyncio.CancelledError if failure == "cancel" else ValueError):
                await task
            assert task.cancelled() == (failure == "cancel")
        assert seen >= 2 and len(opened) == (
            (4 if kind == "manual" else 7) if failure is None else target + 1
        )
        for result in opened:
            result.close.assert_awaited_once()
            assert result.closed
        assert session.get_transaction() is transaction and transaction.is_active
        assert session.in_transaction() and await snapshot(session) == before
        assert not session.new and not session.dirty and not session.deleted


@pytest.mark.parametrize("kind", [*SUMMARY_KINDS, "steps", "sleep"])
@pytest.mark.parametrize("payload_bytes", [0, 65536])
async def test_peak_memory_is_batches_plus_complete_evidence(
    sessions, monkeypatch, kind, payload_bytes
):
    batch = 16
    monkeypatch.setattr(reader, "_RAW_BATCH", batch, raising=False)
    measurements = []
    for first, count in ((0, 128), (128, 1024)):
        rows = []
        for i in range(first, count):
            start, end = T0 + timedelta(seconds=i), T0 + timedelta(seconds=i + 3600)
            if kind == "steps":
                row = steps(str(i), 0, start, start + timedelta(seconds=1))
            elif kind == "sleep":
                row = sleep(
                    str(i),
                    start,
                    end,
                    stages=[
                        {
                            "type": "LIGHT",
                            "startTime": start.isoformat(),
                            "endTime": end.isoformat(),
                        },
                    ],
                )
            else:
                field = next(iter(reader.DAILY_FIELDS[kind][2]))
                row = summary(kind, str(i), **{field: 0})
            # Inside the payload as well as the raw document: listing parsed payloads
            # instead of ORM rows must fail the same memory bound.
            payload = next(value for key, value in row.items() if key not in ("name", "dataSource"))
            payload["syntheticPadding"] = "x" * payload_bytes
            rows.append(row)
        await store(sessions, kind, *rows)
        del rows, row, payload
        async with sessions() as session:
            await reader.canonical_day(session, DAY, "UTC")  # SQL/eligibility warmup
            gc.collect()
            tracemalloc.start()
            try:
                result = await reader.canonical_day(session, DAY, "UTC")
                gc.collect()
                retained, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            [group] = result["streams"]
            assert len(group["observations"]) == count
            if kind == "sleep":
                assert len(group["sleep"]["sessions"]) == count
                assert len(group["sleep"]["timeline"]) == count * 2 - 1
                assert group["value"] == count + 3599
            else:
                assert group["value"] == 0
            assert not session.identity_map and not session.new and not session.dirty
        # Output grows with every observation/session/cell. Budget it separately
        # from transient decoded JSON (two driver batches) and compact sweep state.
        transient = rows_in_flight(sessions, batch) * (payload_bytes + 4096)
        budget = retained + 2 * 1024 * 1024 + count * 8192 + transient + socket_buffers(sessions)
        print(f"canonical {kind=} {count=} {payload_bytes=}: {retained=} {peak=} {budget=}")
        assert peak < budget
        measurements.append(len(json.dumps(result)))
        del result, group
    # Required output growth is visible even when payloads carry no unused fields.
    assert measurements[1] > measurements[0] * 5


async def test_manual_input_memory_is_bounded_beside_complete_output(sessions):
    metrics = ("weight", "height", "body_fat", "muscle")
    output_sizes, queries, loads = [], [], []
    for first, count in ((0, 2048), (2048, 8192)):
        async with sessions.begin() as session:
            await session.execute(
                insert(ManualMeasurement),
                [
                    dict(
                        id=UUID(int=m * 100000 + i + 1),
                        metric=metric,
                        value=Decimal(i) / 10000,
                        measured_at=T0,
                        created_at=T0 + timedelta(seconds=i),
                    )
                    for m, metric in enumerate(metrics)
                    for i in range(first, count)
                ],
            )
        async with sessions.begin() as session:
            before = await snapshot(session)
            stop = reader.bounds(DAY, "UTC")[1]
            await reader._manual_profile(session, stop)  # warm SQL caches
            statements, loaded = [], []

            def trace(conn, cursor, statement, parameters, context, many, recorded=statements):
                recorded.append(statement)

            def track_load(session, instance, recorded=loaded):
                recorded.append(type(instance))

            engine = session.get_bind()
            transaction = session.get_transaction()
            event.listen(engine, "before_cursor_execute", trace)
            event.listen(session.sync_session, "loaded_as_persistent", track_load)
            gc.collect()
            tracemalloc.start()
            try:
                profile = await reader._manual_profile(session, stop)
                gc.collect()
                retained, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
                event.remove(engine, "before_cursor_execute", trace)
                event.remove(session.sync_session, "loaded_as_persistent", track_load)
            queries.append(len(statements))
            loads.append(len(loaded))
            assert all(sql.lstrip().upper().startswith("SELECT") for sql in statements)
            assert [item["metric"] for item in profile] == list(metrics)
            for m, item in enumerate(profile):
                assert item["value"] is None and item["uncertainties"] == ["conflicting_values"]
                assert item["observations"] == [
                    dict(
                        record=str(UUID(int=m * 100000 + i + 1)),
                        value=float(Decimal(i) / 10000),
                        measured_at=T0.isoformat(),
                    )
                    for i in range(count)
                ]
            # Required output is retained separately. Budget only fixed driver batches,
            # socket buffers and small query/conversion overhead as transient input.
            budget = (
                rows_in_flight(sessions, reader._MANUAL_BATCH) * 512
                + socket_buffers(sessions)
                + 256 * 1024
            )
            print(f"canonical manual {count=}: {retained=} {peak=} {budget=}")
            assert peak - retained < budget
            output_sizes.append(retained)
            assert session.get_transaction() is transaction and transaction.is_active
            assert not session.identity_map and not session.new and not session.dirty
            assert await snapshot(session) == before
            del profile, item
    assert queries == [4, 4] and loads == [0, 0]
    assert output_sizes[1] > output_sizes[0] * 3
