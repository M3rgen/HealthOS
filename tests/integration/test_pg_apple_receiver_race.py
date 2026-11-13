"""The Health Auto Export receiver on PostgreSQL: the same body sent twice at the same time.

The phone app retries and sometimes delivers one request twice in parallel. Both deliveries
race for the unique ``apple_payloads.body_sha256``: the first stores the body, the second must
answer ``200 duplicate`` (not 500) and must not write, overwrite or re-archive anything.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from apple_fixtures import hae_body, hae_heart_rate, hae_steps
from sqlalchemy import func, select

from app import main
from app.core.apple_paths import APPLE_RECEIVER_PATH
from app.core.config import Settings
from app.models.apple import AppleImport, ApplePayload, AppleRawRecord
from app.services.providers import apple_archive

pytestmark = pytest.mark.integration

TOKEN = "synthetic-receiver-token-" + "x" * 24


def _application(monkeypatch, sessions, tmp_path):
    token_file = tmp_path / "apple-token"
    token_file.write_text(TOKEN + "\n")
    settings = Settings(
        _env_file=None,
        app_env="test",
        app_ui_token="",
        codex_bridge_token=None,
        apple_receiver_enabled=True,
        apple_receiver_token_file=token_file,
        apple_receiver_requests_per_minute=1_000,
    )
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(
        main,
        "build_runtime",
        lambda *_: SimpleNamespace(webhook_status=SimpleNamespace(record=AsyncMock())),
    )
    application = main.create_app()
    application.state.apple_sessions = sessions
    return application


async def _count(sessions, model) -> int:
    async with sessions() as session:
        return await session.scalar(select(func.count()).select_from(model)) or 0


async def _snapshot(sessions) -> list[tuple]:
    async with sessions() as session:
        rows = await session.execute(
            select(
                ApplePayload.id,
                ApplePayload.received_at,
                ApplePayload.body,
                ApplePayload.body_sha256,
            ).order_by(ApplePayload.id)
        )
        return [tuple(row) for row in rows]


async def test_simultaneous_identical_bodies_answer_ok_and_duplicate(
    monkeypatch, pg_sessions, tmp_path
):
    application = _application(monkeypatch, pg_sessions, tmp_path)
    headers = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
    for round_ in range(5):
        body = hae_body(
            metrics=[
                hae_steps(1000 + round_),
                hae_heart_rate(f"2026-11-0{round_ + 1} 10:00:00 +0100"),
            ]
        )
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as http:
            answers = await asyncio.gather(
                *(http.post(APPLE_RECEIVER_PATH, content=body, headers=headers) for _ in range(2))
            )
        assert [answer.status_code for answer in answers] == [200, 200], [
            answer.text for answer in answers
        ]
        statuses = sorted(answer.json()["status"] for answer in answers)
        assert statuses == ["duplicate", "ok"], statuses
    assert await _count(pg_sessions, ApplePayload) == 5
    # One receiver import per stored body; the duplicate leaves no row behind.
    assert await _count(pg_sessions, AppleImport) == 5
    raw = await _count(pg_sessions, AppleRawRecord)
    assert raw == 10


async def test_racing_store_payload_never_reports_two_stores(pg_sessions):
    """Two transactions storing one body: exactly one reports it stored; the row the first
    wrote is unchanged after the second commits (append-only, nothing overwritten)."""
    body = hae_body(metrics=[hae_steps(4321)])
    first_in = asyncio.Event()

    async def store(wait: bool) -> tuple[int, bool]:
        async with pg_sessions() as session:
            if wait:
                await first_in.wait()
            result = await apple_archive.store_payload(session, body, {})
            if not wait:
                first_in.set()
                # Keep the first transaction open while the second one tries the same body.
                await asyncio.sleep(0.3)
            await session.commit()
            return result

    (first_id, first_stored), (second_id, second_stored) = await asyncio.gather(
        store(False), store(True)
    )
    assert first_id == second_id
    assert (first_stored, second_stored) == (True, False)
    before = await _snapshot(pg_sessions)
    async with pg_sessions() as session:
        again = await apple_archive.store_payload(session, body, {})
        await session.commit()
    assert again == (first_id, False)
    assert await _snapshot(pg_sessions) == before


async def test_two_new_bodies_from_a_new_source_at_once_both_store(
    monkeypatch, pg_sessions, tmp_path
):
    """Different bodies naming a Health source never seen before race for its
    ``apple_sources`` row: the loser is rolled back and run again, not answered 503."""
    application = _application(monkeypatch, pg_sessions, tmp_path)
    headers = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
    bodies = [hae_body(metrics=[hae_steps(100 + n)]) for n in range(2)]
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as http:
        answers = await asyncio.gather(
            *(http.post(APPLE_RECEIVER_PATH, content=body, headers=headers) for body in bodies)
        )
    assert [answer.status_code for answer in answers] == [200, 200], [a.text for a in answers]
    assert [answer.json()["status"] for answer in answers] == ["ok", "ok"]
    assert await _count(pg_sessions, ApplePayload) == 2
    assert await _count(pg_sessions, AppleRawRecord) == 2
    failed = await _failed_imports(pg_sessions)
    assert failed == 0


async def _failed_imports(sessions) -> int:
    async with sessions() as session:
        return (
            await session.scalar(
                select(func.count()).select_from(AppleImport).where(AppleImport.error.is_not(None))
            )
            or 0
        )
