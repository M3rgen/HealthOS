"""Apple export import under load: the API keeps answering, zip bombs are refused (413).

Synthetic exports only. The responsiveness test uploads one large, well compressed export
(tens of MB of XML from a few hundred KB of zip) and asks ``GET /health`` meanwhile on the
same event loop: unzipping and parsing run in a worker thread, so every answer comes back
in well under 200 ms.
"""

from __future__ import annotations

import asyncio
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from apple_fixtures import export_head, export_zip, record, zip_bytes
from sqlalchemy import func, select

from app import main
from app.apple.export_zip import (
    MAX_RATIO,
    ExportTooLarge,
    ZipExportReader,
    open_export_path,
)
from app.apple.upload import ExportUpload
from app.core.apple_paths import APPLE_IMPORT_PATH
from app.core.config import Settings
from app.models.apple import AppleImport, AppleRawRecord
from app.services.providers import apple_import
from app.services.synchronization.pipeline_health import PipelineSnapshot, PipelineStatus

ZIP = {"X-HealthOS-Request": "1", "Content-Type": "application/zip"}
MEMBER = "apple_health_export/export.xml"
TAIL = b"</HealthData>\n"


async def _inspect(**_):
    return PipelineSnapshot(
        status=PipelineStatus.healthy,
        checked_at=datetime.now(UTC),
        last_webhook_received=None,
        last_webhook_processed=None,
        last_sync_success=None,
        latest_frequent_sync=None,
        latest_health_write=None,
        last_reconciliation=None,
        pending_notifications=0,
        failed_notifications=0,
        failed_syncs=0,
    )


def _application(monkeypatch, sessions, tmp_path, **overrides):
    settings = Settings(
        _env_file=None,
        app_env="test",
        app_ui_token="",
        codex_bridge_token=None,
        apple_receiver_token_file=tmp_path / "missing-token",
        **overrides,
    )
    runtime = SimpleNamespace(
        webhook_status=SimpleNamespace(record=AsyncMock()),
        scheduler_needed=lambda _settings: False,
        scheduler_running=False,
        # /health opens its read session from the pipeline's session factory.
        pipeline_health=SimpleNamespace(inspect=_inspect, sessions=sessions),
    )
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "build_runtime", lambda *_: runtime)
    application = main.create_app()
    application.state.apple_sessions = sessions
    return application


def _client(application) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=application)
    return httpx.AsyncClient(transport=transport, base_url="http://localhost", timeout=120)


async def _count(sessions, model) -> int:
    async with sessions() as session:
        return await session.scalar(select(func.count()).select_from(model)) or 0


def _big_export(records: int) -> bytes:
    """An export.xml of ``records`` copies of one step record: heavy to parse, one row to
    archive (the copies are the same record version)."""
    line = f" {record()}\n".encode()
    return export_head().encode() + line * records + TAIL


async def _chunks(data: bytes, size: int = 64 * 1024):
    for offset in range(0, len(data), size):
        yield data[offset : offset + size]


async def test_health_answers_quickly_while_an_export_is_parsed(monkeypatch, sessions, tmp_path):
    xml = _big_export(120_000)
    body = export_zip(xml)
    assert len(xml) > 40 * len(body)  # A few 64 KiB chunks each inflate to megabytes.
    application = _application(monkeypatch, sessions, tmp_path)
    async with _client(application) as http:
        upload = asyncio.create_task(
            http.post(APPLE_IMPORT_PATH, content=_chunks(body), headers=ZIP)
        )
        latencies: list[float] = []
        during = 0
        while not upload.done():
            # A request due 20 ms from now: any time the event loop is blocked past that
            # (a sleep that oversleeps) is time a real /health request would wait.
            started = time.perf_counter()
            await asyncio.sleep(0.02)
            running = apple_import.running() is not None
            answer = await http.get("/health")
            latencies.append(time.perf_counter() - started - 0.02)
            assert answer.status_code == 200, answer.text
            during += running
        response = await upload
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "done"
    assert result["records_read"] == 120_000
    assert result["records_new"] == 1
    # Many /health answers arrived while the import ran. A parse that blocked the event loop
    # would allow only a handful, each as slow as the parse; a shared CI runner may stall one
    # answer, so the typical answer is held to 0.2 s and the worst to 2 s.
    assert during >= 5, (during, latencies)
    assert sorted(latencies)[len(latencies) // 2] < 0.2, sorted(latencies)[-5:]
    assert max(latencies) < 2, max(latencies)


def _bomb(size: int = 80 * 1024**2) -> bytes:
    """A zip whose export.xml is a valid head followed by ``size`` bytes of spaces (deflate
    shrinks it about 1000:1)."""
    xml = export_head().encode() + b" " * size + TAIL
    return zip_bytes([(MEMBER, xml)])


async def test_a_zip_bomb_is_refused_with_413(monkeypatch, sessions, tmp_path):
    body = _bomb()
    application = _application(monkeypatch, sessions, tmp_path)
    async with _client(application) as http:
        response = await http.post(APPLE_IMPORT_PATH, content=body, headers=ZIP)
    assert response.status_code == 413, response.text
    assert "zip bombası" in response.json()["detail"]
    async with sessions() as session:
        row = (await session.scalars(select(AppleImport))).one()
    assert row.status == "failed" and "zip bombası" in (row.error or "")
    assert await _count(sessions, AppleRawRecord) == 0


async def test_the_uncompressed_limit_is_a_setting(monkeypatch, sessions, tmp_path):
    xml = _big_export(20_000)
    body = export_zip(xml)
    limit = 1024 * 1024
    assert len(xml) > limit
    application = _application(monkeypatch, sessions, tmp_path, apple_import_max_uncompressed=limit)
    async with _client(application) as http:
        response = await http.post(APPLE_IMPORT_PATH, content=body, headers=ZIP)
    assert response.status_code == 413, response.text
    assert "1 MB sınırını" in response.json()["detail"]


def test_the_uncompressed_limit_defaults_to_16_gib():
    settings = Settings(_env_file=None, app_env="test")
    assert settings.apple_import_max_uncompressed == 16 * 1024**3


def test_the_ratio_guard_trips_early_and_lets_regular_exports_pass():
    reader = ZipExportReader(ratio_grace=1024 * 1024)
    with pytest.raises(ExportTooLarge) as caught:
        for offset in range(0, len(body := _bomb(8 * 1024**2)), 64 * 1024):
            for _ in reader.iter_feed(body[offset : offset + 64 * 1024]):
                pass
    assert caught.value.reason == "ratio" and caught.value.limit == MAX_RATIO
    # Refused within the grace plus one piece, long before the 8 MiB were inflated.
    assert reader.output_bytes < 2 * 1024 * 1024
    # A regular export (distinct records) is far below the ratio.
    xml = (
        export_head().encode()
        + b"".join(
            f" {record(value=str(n), start=f'2026-11-06 10:{n % 60:02d}:00 +0100')}\n".encode()
            for n in range(5000)
        )
        + TAIL
    )
    upload = ExportUpload()
    data = export_zip(xml)
    records = upload.feed(data) + upload.close()
    assert len(records) == 5000


def test_streamed_records_are_never_collected_whole():
    """``iter_feed`` hands over records batch by batch: a single chunk inflating to many MB
    never yields one list of every record in it."""
    xml = _big_export(50_000)
    upload = ExportUpload()
    largest = 0
    total = 0
    for batch in upload.iter_feed(export_zip(xml)):
        largest = max(largest, len(batch))
        total += len(batch)
    for batch in upload.iter_close():
        largest = max(largest, len(batch))
        total += len(batch)
    assert total == 50_000
    assert largest < 5_000


def test_the_cli_reader_holds_the_same_limits(tmp_path: Path):
    path = tmp_path / "export.zip"
    path.write_bytes(_bomb(70 * 1024**2))
    with pytest.raises(ExportTooLarge) as caught:
        for _ in open_export_path(path):
            pass
    assert caught.value.reason == "ratio"
    path.write_bytes(export_zip(_big_export(20_000)))
    with pytest.raises(ExportTooLarge) as caught:
        for _ in open_export_path(path, max_output=1024 * 1024):
            pass
    assert caught.value.reason == "output"
    with zipfile.ZipFile(path) as archive:
        assert archive.getinfo(MEMBER).file_size > 1024 * 1024
