"""POST /api/apple/import and GET /api/apple/status (synthetic exports only)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from apple_fixtures import chunked, export_xml, export_zip, full_export, record
from sqlalchemy import func, select

from app import main
from app.core.apple_paths import APPLE_IMPORT_PATH
from app.core.config import Settings
from app.models.apple import AppleImport, AppleRawRecord
from app.services import apple_import

JOB = {"X-HealthOS-Request": "1"}
ZIP = {**JOB, "Content-Type": "application/zip"}
XML = {**JOB, "Content-Type": "application/xml"}


@pytest.fixture
def application(monkeypatch, sessions, tmp_path):
    settings = Settings(
        _env_file=None,
        app_env="test",
        app_ui_token="",
        codex_bridge_token=None,
        apple_receiver_token_file=tmp_path / "missing-token",
        apple_import_max_upload_bytes=2 * 1024 * 1024,
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


@pytest.fixture
async def http(application):
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        yield client


async def count(sessions, model) -> int:
    async with sessions() as session:
        return await session.scalar(select(func.count()).select_from(model)) or 0


async def test_zip_upload_archives_and_reports(http, sessions):
    body = export_zip(full_export())
    response = await http.post(
        APPLE_IMPORT_PATH,
        content=body,
        headers={**ZIP, "X-Apple-Export-Filename": "d%C4%B1%C5%9Fa%20aktar%C4%B1lan.zip"},
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "done"
    assert result["origin"] == "upload" and result["channel"] == "export"
    assert result["filename"] == "dışa aktarılan.zip"
    assert result["bytes_read"] == len(body)
    assert result["records_read"] == result["records_new"] > 5
    assert result["rows_written"] > 0
    assert result["export_date"] is not None
    assert result["summary"]["format"] == "zip"
    assert result["summary"]["member"].endswith("export.xml")
    assert result["summary"]["skipped"]
    assert response.headers["cache-control"] == "no-store"
    assert await count(sessions, AppleRawRecord) == result["records_new"]


async def test_reupload_adds_nothing_new(http, sessions):
    xml = full_export()
    first = await http.post(APPLE_IMPORT_PATH, content=xml, headers=XML)
    again = await http.post(APPLE_IMPORT_PATH, content=export_zip(xml), headers=ZIP)
    assert first.status_code == again.status_code == 200
    assert again.json()["records_read"] == first.json()["records_read"]
    assert again.json()["records_new"] == 0
    assert again.json()["rows_written"] == 0
    assert await count(sessions, AppleRawRecord) == first.json()["records_new"]


async def test_streamed_upload_in_small_batches(http, sessions, monkeypatch):
    monkeypatch.setattr(apple_import, "BATCH_RECORDS", 2)
    xml = export_xml(*(record(value=str(100 + n)) for n in range(7)))

    async def chunks():
        for piece in chunked(export_zip(xml), 97):
            yield piece

    batches = []
    archive = apple_import._archive_batch

    async def counted(sessions, import_id, batch, progress):
        batches.append(len(batch))
        await archive(sessions, import_id, batch, progress)

    monkeypatch.setattr(apple_import, "_archive_batch", counted)
    response = await http.post(APPLE_IMPORT_PATH, content=chunks(), headers=ZIP)
    assert response.status_code == 200, response.text
    assert response.json()["records_new"] == 7
    assert batches == [2, 2, 2, 1]
    async with sessions() as session:
        row = (await session.scalars(select(AppleImport))).one()
    assert row.records_read == 7 and row.status == "done"


async def test_declared_length_over_the_limit_is_413_without_a_row(http, sessions):
    response = await http.post(
        APPLE_IMPORT_PATH,
        content=b"x" * 16,
        headers={**ZIP, "Content-Length": str(3 * 1024 * 1024)},
    )
    assert response.status_code == 413
    assert await count(sessions, AppleImport) == 0


async def test_streamed_bytes_over_the_limit_are_413_and_marked_failed(http, sessions):
    async def chunks():
        # A well-formed start (the root still open), then more than the limit of comments.
        yield export_xml(record()).split(b"</HealthData>")[0]
        for _ in range(40):
            yield b"<!-- " + b"x" * (64 * 1024) + b" -->"

    response = await http.post(APPLE_IMPORT_PATH, content=chunks(), headers=XML)
    assert response.status_code == 413
    async with sessions() as session:
        row = (await session.scalars(select(AppleImport))).one()
    assert row.status == "failed" and "MB" in (row.error or "")


@pytest.mark.parametrize(
    "body",
    [
        b"this is not an export at all, just some text of sufficient length......",
        b"PK\x03\x04" + b"\x00" * 200,
        b"<?xml version='1.0'?><HealthData locale='en_US'><Record",
    ],
)
async def test_garbage_is_400_and_marked_failed(http, sessions, body):
    response = await http.post(APPLE_IMPORT_PATH, content=body, headers=ZIP)
    assert response.status_code == 400
    assert response.json()["detail"].startswith("Apple Sağlık dışa aktarması okunamadı")
    async with sessions() as session:
        row = (await session.scalars(select(AppleImport))).one()
    assert row.status == "failed"


async def test_unsupported_content_type_is_415(http, sessions):
    response = await http.post(
        APPLE_IMPORT_PATH, content=b"{}", headers={**JOB, "Content-Type": "application/json"}
    )
    assert response.status_code == 415
    assert await count(sessions, AppleImport) == 0


@pytest.mark.parametrize(
    "headers",
    [
        {"Content-Type": "application/zip"},
        {**ZIP, "X-HealthOS-Request": "0"},
        {**ZIP, "Origin": "http://attacker.example"},
        {**ZIP, "Sec-Fetch-Site": "cross-site"},
    ],
)
async def test_csrf_check_runs_before_reading(http, sessions, headers):
    response = await http.post(
        APPLE_IMPORT_PATH, content=export_zip(full_export()), headers=headers
    )
    assert response.status_code == 403
    assert await count(sessions, AppleImport) == 0


async def test_same_origin_browser_upload_is_allowed(http):
    response = await http.post(
        APPLE_IMPORT_PATH,
        content=full_export(),
        headers={**XML, "Origin": "http://localhost", "Sec-Fetch-Site": "same-origin"},
    )
    assert response.status_code == 200


async def test_second_concurrent_upload_is_409_and_status_shows_progress(http, sessions):
    xml = full_export()
    release = asyncio.Event()
    sent = asyncio.Event()

    async def slow():
        yield xml[: len(xml) // 2]
        sent.set()
        await release.wait()
        yield xml[len(xml) // 2 :]

    first = asyncio.create_task(http.post(APPLE_IMPORT_PATH, content=slow(), headers=XML))
    await asyncio.wait_for(sent.wait(), 5)
    for _ in range(50):
        if apple_import.running() is not None and apple_import.running()["bytes_read"]:
            break
        await asyncio.sleep(0.01)
    second = await http.post(APPLE_IMPORT_PATH, content=xml, headers=XML)
    status = await http.get("/api/apple/status")
    release.set()
    done = await asyncio.wait_for(first, 10)
    assert second.status_code == 409
    assert done.status_code == 200
    running = status.json()["running"]
    assert running["origin"] == "upload"
    assert running["bytes_read"] == len(xml) // 2
    assert set(running) >= {"import_id", "records_read", "records_new", "started_at"}
    assert apple_import.running() is None
    # The refused upload left no import row.
    assert await count(sessions, AppleImport) == 1


async def test_failed_import_releases_the_guard(http):
    bad = await http.post(APPLE_IMPORT_PATH, content=b"not an export" * 10, headers=XML)
    good = await http.post(APPLE_IMPORT_PATH, content=full_export(), headers=XML)
    assert bad.status_code == 400
    assert good.status_code == 200


async def test_status_shape(http, sessions):
    empty = await http.get("/api/apple/status")
    assert empty.status_code == 200
    assert empty.headers["cache-control"] == "no-store"
    body = empty.json()
    assert set(body) == {"receiver", "running", "imports", "sources"}
    assert body["receiver"] == {
        "enabled": False,
        "token_file_present": False,
        "endpoint_configured": False,
        "path": "/webhooks/apple-health",
    }
    assert body["running"] is None and body["imports"] == []
    await http.post(APPLE_IMPORT_PATH, content=full_export(), headers=XML)
    await http.post(APPLE_IMPORT_PATH, content=b"garbage" * 20, headers=XML)
    after = (await http.get("/api/apple/status")).json()
    assert [item["status"] for item in after["imports"]] == ["failed", "done"]
    assert "Test Watch" in [item["source_name"] for item in after["sources"]]
    assert set(after["imports"][0]) >= {
        "id",
        "channel",
        "origin",
        "status",
        "filename",
        "started_at",
        "finished_at",
        "bytes_read",
        "records_read",
        "records_new",
        "rows_written",
        "export_date",
        "error",
        "summary",
    }
    # Bookkeeping only: no record bodies in the status.
    assert "creationDate" not in (await http.get("/api/apple/status")).text


async def test_status_lists_at_most_ten_imports(http):
    for _ in range(12):
        await http.post(APPLE_IMPORT_PATH, content=b"garbage" * 20, headers=XML)
    assert len((await http.get("/api/apple/status")).json()["imports"]) == 10
