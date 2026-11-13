"""Runtime guards must retain their error paths with Python optimization enabled."""

import ast
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import Column, Integer, MetaData, Numeric, String, Table
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.normalization import from_milli, prepare_numeric
from app.google_health.client import GoogleHealthClient
from app.google_health.data_types import REGISTRY
from app.google_health.oauth_response import read_token_body
from app.google_health.pagination import PaginationPaused
from app.google_health.response_body import read_wire
from app.services.interpretation import derived
from app.services.providers.ingestion import GoogleSource
from app.services.synchronization import coverage, coverage_reading, webhook_metadata
from app.services.umay.codex_runtime_rpc import AppServer, ProbeError
from app.sync import backfill, incremental
from app.sync.ownership import OwnershipLost


def test_production_has_no_optimization_dependent_guards():
    root = Path(__file__).resolve().parents[1] / "app"
    violations = [
        f"{path.relative_to(root)}:{node.lineno}"
        for path in root.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Assert)
    ]
    assert not violations, violations


@pytest.mark.parametrize("value", ["NaN", "sNaN", "Infinity", "-Infinity"])
def test_milli_conversion_rejects_nonfinite_values(value):
    with pytest.raises(InvalidOperation, match="Nonfinite source number"):
        from_milli(Decimal(value))


@pytest.mark.parametrize("kind", [Numeric(), Numeric(precision=8), Numeric(scale=2)])
def test_numeric_columns_require_explicit_bounds(kind):
    table = Table("synthetic", MetaData(), Column("value", kind))
    with pytest.raises(RuntimeError, match="explicit precision and scale"):
        prepare_numeric(SimpleNamespace(__table__=table), {"value": Decimal("1")})


@pytest.mark.parametrize("operation", ["read", "send"])
@pytest.mark.parametrize("started", [False, True])
async def test_bridge_probe_rejects_unavailable_pipes(tmp_path, operation, started):
    server = AppServer([], tmp_path, {}, tmp_path / "probe.log", {})
    if started:
        server.process = SimpleNamespace(stdin=None, stdout=None)
    with pytest.raises(ProbeError, match="app-server .* is unavailable"):
        if operation == "read":
            await server.read()
        else:
            await server.send({})


class SyncOnlyStream(httpx.SyncByteStream):
    def __iter__(self):
        return iter([b"synthetic"])


async def test_wire_reader_rejects_sync_stream():
    response = httpx.Response(200, stream=SyncOnlyStream())
    body = bytearray()
    with pytest.raises(RuntimeError, match="asynchronous response stream"):
        await read_wire(response, body)
    assert not body


async def test_token_reader_sanitizes_sync_stream_failure():
    response = httpx.Response(200, stream=SyncOnlyStream())
    with pytest.raises(httpx.TransportError, match="OAuth token endpoint connection failed"):
        await read_token_body(response, float("inf"))


async def test_archive_write_requires_archive():
    async with httpx.AsyncClient() as http:
        client = GoogleHealthClient(Settings(_env_file=None, app_env="test"), None, http=http)
        with pytest.raises(RuntimeError, match="Response archive is unavailable"):
            await client._store([])


async def test_derived_registration_must_exist(sessions, monkeypatch):
    monkeypatch.setattr(derived, "register_range", AsyncMock())
    async with sessions.begin() as session:
        with pytest.raises(RuntimeError, match="Derived day registration is missing"):
            await derived.rebuild_day(session, date(2026, 1, 1), "UTC")


@pytest.mark.parametrize("service", [incremental, backfill])
async def test_sync_does_not_write_without_ownership(sessions, monkeypatch, service):
    @asynccontextmanager
    async def unowned(*_args, **_kwargs):
        yield None

    monkeypatch.setattr(service, "run_ownership", unowned)
    settings = Settings(_env_file=None, app_env="test")
    cls = incremental.IncrementalSync if service is incremental else backfill.BackfillService
    sync = cls(settings, sessions, None, {"steps": REGISTRY["steps"]})
    with pytest.raises(OwnershipLost, match="ownership was not acquired"):
        await sync.run("steps" if service is incremental else ["steps"])


@pytest.mark.parametrize("failure", [None, PaginationPaused("budget"), RuntimeError("synthetic")])
async def test_sync_reports_missing_state_after_fetch(sessions, monkeypatch, failure):
    sync = incremental.IncrementalSync(
        Settings(_env_file=None, app_env="test"),
        sessions,
        GoogleSource(None),
        {"steps": REGISTRY["steps"]},
    )
    fetch = AsyncMock(side_effect=failure)
    monkeypatch.setattr(sync, "_fetch_round", fetch)
    monkeypatch.setattr(AsyncSession, "get", AsyncMock(return_value=None))
    with pytest.raises(RuntimeError, match="Incremental sync state is missing"):
        await sync.run("steps")
    fetch.assert_awaited_once()


@pytest.mark.parametrize("kind", [String(), Integer()])
def test_webhook_index_requires_a_bounded_string(monkeypatch, kind):
    table = Table("synthetic", MetaData(), Column("data_type", kind))
    monkeypatch.setattr(webhook_metadata, "WebhookEvent", SimpleNamespace(__table__=table))
    with pytest.raises(RuntimeError, match="bounded string type"):
        webhook_metadata.index_limit("dataType")


async def test_normalized_coverage_requires_google_data_type():
    day = date(2026, 1, 1)
    window = coverage.Window(day, day, day, ZoneInfo("UTC"))
    with pytest.raises(ValueError, match="requires a Google data type"):
        await coverage_reading.google_streams(None, coverage.Metric("steps", None, "steps"), window)
