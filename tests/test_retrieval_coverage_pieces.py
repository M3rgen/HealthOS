"""HTTP evidence splitting with real synthetic receipt lineages and timestamp reads."""

import weakref
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from retrieval_records import END, START, Client, Runner, point
from sqlalchemy import Text, cast, func, select
from support.coverage_fixtures import PATH, QUERY, fetch
from support.coverage_fixtures import api as api

from app.google_health.client import Page
from app.google_health.data_types import CIVIL_AFTER, CIVIL_BEFORE, REGISTRY
from app.models.retrieval import RetrievalEvent as Event
from app.services.synchronization import retrieval
from app.services.synchronization import retrieval_coverage as coverage


@pytest.fixture
def reads(monkeypatch):
    original = coverage.evidence
    calls, previous = [], []

    class Times(dict):
        pass

    async def tracked(session, name, window):
        # Successful pieces must release both ORM rows and the timestamp mapping
        # before loading the next piece (or the next type).
        assert all(reference() is None for reference in previous)
        calls.append((name, window.first, window.end))
        attempts, times = await original(session, name, window)
        times = Times(times)
        previous[:] = [weakref.ref(times)] + [
            weakref.ref(row) for receipt, pages, _, _ in attempts for row in [receipt, *pages]
        ]
        return attempts, times

    monkeypatch.setattr(coverage, "evidence", tracked)
    return calls


async def timestamp_budget(sessions):
    async with sessions() as session:
        return await session.scalar(select(func.max(func.length(cast(Event.returned_times, Text)))))


async def report(api, **overrides):
    params = {key: value for key, value in (QUERY | overrides).items() if value is not None}
    response = await api.get(PATH, params=params)
    assert response.status_code == 200, response.text
    assert len(response.content) <= 12 * 1024
    return response.json()


@pytest.mark.parametrize("budget", ["MAX_RECEIPTS", "MAX_EVENTS", "MAX_EVIDENCE_BYTES"])
async def test_long_window_matches_separate_queries_and_type_pages(
    api, sessions, monkeypatch, reads, budget
):
    kinds = ("heart-rate", "heart-rate-variability")
    for kind in kinds:
        await fetch(sessions, kind=kind, points=[point(kind=kind)])
        await fetch(
            sessions,
            kind=kind,
            start=START + timedelta(days=19),
            end=END + timedelta(days=19),
            points=[point("2026-09-20T12:00:00Z", kind), {"unrecognized": "synthetic"}],
        )
        with pytest.raises(RuntimeError):
            await Runner(
                sessions, "sync", kind, START + timedelta(days=9), END + timedelta(days=9)
            ).run(Client(RuntimeError("synthetic failure")))
    expected = [await report(api, data_type=kind, end_date="2026-09-20") for kind in kinds]
    ceilings = {
        "MAX_RECEIPTS": 1,
        "MAX_EVENTS": 2,
        "MAX_EVIDENCE_BYTES": await timestamp_budget(sessions),
    }
    monkeypatch.setattr(coverage, budget, ceilings[budget])
    reads.clear()
    offset = sorted(REGISTRY).index(kinds[0])
    combined = await report(api, data_type=None, end_date="2026-09-20", offset=offset, limit=2)
    assert len(reads) > 2
    assert combined["types"] == [item["types"][0] for item in expected]
    assert combined["next_offset"] == offset + 2
    counts = ("days_without_value", "unknown_days", "incomplete_days", "not_attempted_days")
    for key in counts:
        assert combined["summary"][key] == sum(item["summary"][key] for item in expected)
    for kind, item in zip(kinds, expected, strict=True):
        left = await report(api, data_type=kind, end_date="2026-09-10")
        right = await report(api, data_type=kind, start_date="2026-09-11", end_date="2026-09-20")
        assert item["types"][0]["days"] == left["types"][0]["days"] + right["types"][0]["days"]
        assert item["types"][0]["days"][9]["retrieval"] == "incomplete"
        assert item["types"][0]["days"][-1]["reason"] == "returned_dates_unknown"
        for key in counts:
            assert item["summary"][key] == left["summary"][key] + right["summary"][key]


@pytest.mark.parametrize(
    "budget,code",
    [
        ("MAX_RECEIPTS", "coverage_receipt_limit"),
        ("MAX_EVENTS", "coverage_evidence_limit"),
        ("MAX_EVIDENCE_BYTES", "coverage_evidence_limit"),
    ],
)
@pytest.mark.parametrize("exhausted", [False, True])
async def test_single_day_failure_and_total_attempt_cap_are_structured_413(
    api, sessions, monkeypatch, reads, budget, code, exhausted
):
    for shift in (0, 19):
        await fetch(
            sessions,
            start=START + timedelta(days=shift),
            end=END + timedelta(days=shift),
            points=[point((START + timedelta(days=shift, hours=12)).isoformat())],
        )
    ceilings = {
        "MAX_RECEIPTS": 1,
        "MAX_EVENTS": 2,
        "MAX_EVIDENCE_BYTES": await timestamp_budget(sessions),
    }
    monkeypatch.setattr(coverage, budget, ceilings[budget] if exhausted else 0)
    if exhausted:
        # Full range fails, left half succeeds, then the cap refuses the right half.
        monkeypatch.setattr(coverage, "MAX_EVIDENCE_PIECES", 2)
    response = await api.get(PATH, params=QUERY | {"end_date": "2026-09-20"})
    assert response.status_code == 413
    assert response.json()["detail"] == {
        "code": code,
        "hint": "Tarih aralığını veya limit değerini azalt.",
    }
    assert len(reads) == (2 if exhausted else 6)
    if not exhausted:
        assert reads[-1][1] == reads[-1][2] == START.date()


@pytest.mark.parametrize("kind", ["heart-rate", "daily-resting-heart-rate", "exercise"])
@pytest.mark.parametrize("dst_day,hours", [("2026-03-29", 23), ("2026-10-25", 25)])
@pytest.mark.parametrize("outcome", ["complete", "unfinished", "early_snapshot"])
async def test_split_keeps_resumed_chain_snapshot_civil_envelope_and_dst_days(
    api, sessions, monkeypatch, reads, kind, dst_day, hours, outcome
):
    zone = ZoneInfo("Europe/Brussels")
    local = datetime.fromisoformat(dst_day).replace(tzinfo=zone)
    assert (
        (local + timedelta(days=1)).astimezone(UTC) - local.astimezone(UTC)
    ).total_seconds() == hours * 3600
    first, stop = local - timedelta(days=2), local + timedelta(days=3)
    now = (stop + timedelta(days=1)).astimezone(UTC)
    if outcome == "early_snapshot":
        now = (first + timedelta(hours=12)).astimezone(UTC)

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz or UTC)

    monkeypatch.setattr(retrieval, "datetime", Frozen)
    monkeypatch.setattr(coverage, "datetime", Frozen)
    start, end = first.astimezone(UTC), stop.astimezone(UTC)
    if kind == "exercise":
        start, end = start - CIVIL_BEFORE, end + CIVIL_AFTER
    runner = Runner(sessions, "sync", kind, start, end)

    def record(at):
        payload = point(at.isoformat(), kind)
        if kind == "exercise":
            payload["exercise"] = {
                "interval": {
                    "startTime": at.isoformat(),
                    "endTime": (at + timedelta(hours=1)).isoformat(),
                }
            }
        return payload

    with pytest.raises(RuntimeError):
        await runner.run(
            Client(
                Page([record(first + timedelta(hours=12))], "second"),
                RuntimeError("synthetic interruption"),
            )
        )
    now = (stop + timedelta(days=1)).astimezone(UTC)
    pages = [
        Page([record(local + timedelta(hours=12))], "third"),
        Page([record(stop - timedelta(hours=12))], "fourth" if outcome == "unfinished" else None),
    ]
    if outcome == "unfinished":
        with pytest.raises(RuntimeError):
            await runner.run(Client(*pages, RuntimeError("synthetic unfinished chain")))
    else:
        await runner.run(Client(*pages))
    params = {
        "data_type": kind,
        "start_date": first.date().isoformat(),
        "end_date": (stop.date() - timedelta(days=1)).isoformat(),
        "timezone": zone.key,
    }
    expected = await report(api, **params)
    monkeypatch.setattr(coverage, "MAX_EVIDENCE_BYTES", await timestamp_budget(sessions))
    reads.clear()
    actual = await report(api, **params)
    assert len(reads) > 1
    assert actual == expected
    days = actual["types"][0]["days"]
    assert days[2]["retrieval"] == (
        "completed_with_records" if outcome == "complete" else "incomplete"
    )
    assert days[2]["returned_records"] == (1 if outcome == "complete" else None)
    assert days[3]["retrieval"] == ("completed_empty" if outcome == "complete" else "incomplete")
    assert days[3]["returned_records"] == (0 if outcome == "complete" else None)
    for entry in days:
        separate = await report(
            api, **(params | {"start_date": entry["date"], "end_date": entry["date"]})
        )
        assert separate["types"][0]["days"] == [entry]
