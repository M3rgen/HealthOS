"""Synthetic HTTP pagination failures preserve evidence and resumable kind state."""

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from garmin_records import FakeGarmin, activity
from sqlalchemy import select
from test_garmin_pull import TODAY, Clock, count, service
from test_garmin_pull import key_file as key_file

from app.core.fetch_window import PaginationPaused
from app.models.garmin import GarminApiResponse, GarminRawRecord, GarminRecordObservation
from app.services.providers import garmin_ingestion, garmin_kinds
from app.services.providers.garmin_ingestion import GarminPaginationError
from app.services.providers.garmin_kinds import GarminResponseShapeError
from app.services.providers.garmin_pull import BY_NAME, day_window

PATH = "/activitylist-service/activities/search/activities"
A = [activity(1, TODAY), activity(2, TODAY)]
B = [activity(3, TODAY), activity(4, TODAY)]
FAILURES = {
    "shape-first": [{"error": "synthetic"}],
    "shape-later": [A, {"error": "synthetic"}],
    "repeat": [A, A],
    "reorder": [A, [dict(reversed(list(row.items()))) for row in reversed(A)]],
    "cycle": [A, B, A],
    "limit": [
        A,
        B,
        [activity(5, TODAY), activity(6, TODAY)],
        [activity(7, TODAY), {"note": "unstorable\u0000\ud800"}],
    ],
}


@pytest.fixture
async def paginated(sessions, key_file, monkeypatch):
    monkeypatch.setattr(garmin_ingestion, "ACTIVITY_PAGE", 2)
    monkeypatch.setattr(garmin_kinds, "ACTIVITY_PAGE", 2)
    monkeypatch.setattr(garmin_ingestion, "MAX_ACTIVITY_PAGES", 4)
    fake, clock = FakeGarmin(), Clock()
    pull = service(sessions, fake, key_file, clock)
    yield pull, fake, clock
    await pull.aclose()


def respond(fake, pages):
    fake.requests.clear()
    fake.bodies[PATH] = lambda request: pages[int(request.url.params["start"]) // 2]


def requests(fake):
    return [request for request in fake.requests if request.url.path == PATH]


@pytest.mark.parametrize("case", FAILURES)
async def test_direct_source_yields_stopped_response_before_typed_safe_failure(paginated, case):
    pull, fake, _ = paginated
    pages = FAILURES[case]
    respond(fake, pages)
    budget = SimpleNamespace(remaining=len(pages))
    yielded = []
    malformed = case.startswith("shape-")
    with pytest.raises(GarminResponseShapeError if malformed else GarminPaginationError) as error:
        async for page in pull.source.fetch(
            BY_NAME["activities"], day_window(TODAY, TODAY), budget=budget
        ):
            yielded.append(page)
    assert [page.data_points for page in yielded] == (pages[:-1] if malformed else pages)
    if yielded:
        assert yielded[-1].next_page_token is not None
    if malformed:
        async with pull.sessions() as session:
            kept = (await session.scalars(select(GarminApiResponse))).all()
            assert len(kept) == 1 + int(case == "shape-first")  # Token only on first page.
            assert kept[-1].body == json.dumps(pages[-1]).encode()
    else:
        assert yielded[-1].exchanges[-1].body == json.dumps(pages[-1]).encode()
    assert len(requests(fake)) == len(pages) and budget.remaining == int(malformed)
    assert str(error.value) == (
        "Garmin list response has an unexpected structure"
        if malformed
        else "Garmin activity search exceeded its page limit"
        if case == "limit"
        else "Garmin activity search returned a repeated full page"
    )
    assert error.value.__cause__ is None and error.value.__context__ is None


@pytest.mark.parametrize(
    "pages",
    [
        [A, B, []],
        [A, B, [activity(5, TODAY)]],
        [A, [A[1], B[0]], []],  # Partial overlap.
        [A, [{**row, "calories": 300} for row in A], []],  # Same IDs, new content.
        [[A[0], A[0]], A, [A[1], A[1]], []],  # Multiplicity, short page at the cap.
    ],
)
async def test_distinct_pages_finish_and_history_resets_per_fetch(paginated, pages):
    pull, fake, _ = paginated
    for _ in range(2):
        respond(fake, pages)
        fetched = [
            page
            async for page in pull.source.fetch(BY_NAME["activities"], day_window(TODAY, TODAY))
        ]
        assert [page.data_points for page in fetched] == pages
        assert fetched[-1].next_page_token is None
        assert [int(r.url.params["start"]) for r in requests(fake)] == list(
            range(0, 2 * len(pages), 2)
        )


async def test_smaller_caller_budget_still_pauses(paginated):
    pull, fake, _ = paginated
    respond(fake, [A, B])
    with pytest.raises(PaginationPaused):
        async for _ in pull.source.fetch(
            BY_NAME["activities"],
            day_window(TODAY, TODAY),
            budget=SimpleNamespace(remaining=1),
        ):
            pass
    assert len(requests(fake)) == 1


async def test_activity_search_pages_until_a_short_page(sessions, paginated):
    pull, fake, _ = paginated
    respond(fake, [A, [B[0]]])
    summary = await pull.run(["activities"])
    assert not summary.failed
    assert summary.kinds["activities"].requests == 2
    assert summary.kinds["activities"].new_records == 3
    assert await count(sessions, GarminRawRecord) == 3
    assert await count(sessions, GarminRecordObservation) == 3


async def evidence(sessions):
    async with sessions() as session:
        return {
            model: (await session.execute(select(model.__table__).order_by(model.id))).all()
            for model in (GarminApiResponse, GarminRawRecord, GarminRecordObservation)
        }


@pytest.mark.parametrize("backfill", [False, True])
@pytest.mark.parametrize("case", FAILURES)
async def test_pull_failure_keeps_evidence_and_retries_failed_chunk(
    sessions, paginated, backfill, case
):
    pull, fake, clock = paginated
    respond(fake, [[activity(100, TODAY)]])
    assert not (await pull.run(["activities"])).failed
    before, success = await evidence(sessions), clock.now.isoformat()
    clock.now += timedelta(minutes=1)
    since = TODAY - timedelta(days=90)
    pages = FAILURES[case]
    respond(fake, pages)
    failing = fake.bodies[PATH]
    # Complete one backfill chunk, then fail the next; the checkpoint stays at TODAY.
    fake.bodies[PATH] = lambda r: [] if r.url.params["startDate"] == str(since) else failing(r)
    if backfill:
        result = await pull.backfill(since, TODAY, kinds=["activities", "steps"])
        entry = result["kinds"]["activities"]
        assert result["failed"] and not entry["complete"]
    else:
        result = await pull.run(["activities", "steps"])
        entry = result.kinds["activities"].as_dict()
        assert result.failed
    assert entry["error"].startswith(
        "Garmin list response" if case.startswith("shape-") else "Garmin activity search"
    )
    assert entry["requests"] == len(pages) + int(backfill)
    status = await pull.status()
    state = status["kinds"]["activities"]
    assert status["failing_kinds"] == ["activities"]
    assert state["last_success_at"] == success
    assert state["last_error_at"] is not None and not status["kinds"]["steps"]["failing"]
    assert status["kinds"]["steps"]["last_success_at"] is not None
    if backfill:
        assert state["backfill"]["next"] == str(TODAY) and not state["backfill"]["complete"]
    after = await evidence(sessions)
    for model, rows in before.items():
        assert after[model][: len(rows)] == rows
    responses = [r for r in after[GarminApiResponse] if r.kind == "activities"]
    stopped = responses[-1]
    assert len(responses) == 1 + len(pages) + int(backfill)
    assert stopped.status_code == 200 and stopped.complete
    assert stopped.body == json.dumps(pages[-1]).encode()
    observations = [r for r in after[GarminRecordObservation] if r.response_id == stopped.id]
    malformed = case.startswith("shape-")
    assert len(observations) == (0 if malformed else 1 if case == "limit" else 2)
    assert entry["records"] == 2 * (len(pages) - int(malformed))
    assert entry["new_records"] == (
        0 if case == "shape-first" else 7 if case == "limit" else 4 if case == "cycle" else 2
    )
    # A healthy retry re-reads from offset zero, including the failed date chunk.
    clock.now += timedelta(minutes=1)
    respond(fake, [A, []])
    if backfill:
        recovered = await pull.backfill(since, TODAY, kinds=["activities", "steps"])
        assert not recovered["failed"]
        assert recovered["kinds"]["activities"]["resumed_from"] == str(TODAY)
        assert recovered["kinds"]["activities"]["complete"]
        assert recovered["kinds"]["steps"]["requests"] == 0
    else:
        assert not (await pull.run(["activities", "steps"])).failed
    assert [r.url.params["start"] for r in requests(fake)] == ["0", "2"]
    assert (await pull.status())["failing_kinds"] == []
    assert (await pull.status())["kinds"]["activities"]["last_success_at"] > state["last_error_at"]


@pytest.mark.parametrize("case", ["repeat", "limit"])
async def test_stopped_page_write_rolls_back_and_keep_after_retains_http(
    sessions, paginated, monkeypatch, case
):
    pull, fake, _ = paginated
    pages = FAILURES[case]
    respond(fake, pages)
    real = pull.backend.store_raw
    committed = None

    async def fail_last(session, stream, page, **kwargs):
        nonlocal committed
        if page.next_page_token == str(len(pages)):
            committed = await evidence(sessions)
            await real(session, stream, page, **kwargs)
            raise RuntimeError("synthetic write failure")
        return await real(session, stream, page, **kwargs)

    monkeypatch.setattr(pull.backend, "store_raw", fail_last)
    summary = await pull.run(["activities"])
    assert summary.failed and summary.kinds["activities"].error == "RuntimeError"
    assert len(requests(fake)) == len(pages)
    after = await evidence(sessions)
    for model in (GarminRawRecord, GarminRecordObservation):
        assert after[model] == committed[model]
    assert await count(sessions, GarminApiResponse) == len(pages) + 1  # Token plus pages.
    stopped = after[GarminApiResponse][-1]
    assert stopped.body == json.dumps(pages[-1]).encode()
    assert not [r for r in after[GarminRecordObservation] if r.response_id == stopped.id]
    assert (await pull.status())["kinds"]["activities"]["last_success_at"] is None
