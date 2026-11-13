"""Provider calls are interchangeable; incompatible signatures fail static checking."""

import ast
import json
import os
import subprocess
import sys
from asyncio import CancelledError
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_services_keep_google_transport_behind_provider_boundaries():
    imports = []
    for path in (ROOT / "app/services").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = (
                [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else []
            )
            if any(
                name == "app.google_health" or name.startswith("app.google_health.")
                for name in names
            ):
                imports.append(path.name)
                break
    # Issue #336 baseline: 39 of 138 modules. Keep at least the requested halving.
    assert len(imports) <= 19, sorted(imports)


def test_both_providers_satisfy_the_callable_protocol_under_pyright(tmp_path):
    positive = tmp_path / "providers.py"
    positive.write_text(
        """from typing import assert_type
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.fetch_window import FetchWindow
from app.core.ingestion_stats import NormalizeStats, StoreStats
from app.google_health.client import Page
from app.services.providers.garmin_ingestion import GarminPage, GarminSource
from app.services.providers.ingestion import GoogleSource
from app.services.readers.reading import BoundsReader
from app.services.interpretation.google_daily_readers import archive_bounds as google_bounds
from app.services.providers.garmin_readers import archive_bounds as garmin_bounds
from app.services.providers.ingestion_contracts import (
    IngestionPage, IngestionSource, Stream, StreamStatus, ingest_pages,
)

google_reader: BoundsReader = google_bounds
garmin_reader: BoundsReader = garmin_bounds

def google(source: GoogleSource) -> IngestionSource[Page]:
    return source

def garmin(source: GarminSource) -> IngestionSource[GarminPage]:
    return source

async def calls[P: IngestionPage](
    source: IngestionSource[P], session: AsyncSession, stream: Stream, window: FetchWindow,
) -> None:
    async def commit(page: P) -> StoreStats:
        return await source.store_raw(session, stream, page)
    async for page, stats in ingest_pages(source, stream, window, commit):
        assert_type(page, P)
        assert_type(stats, StoreStats)
    assert_type(await source.normalize(window), NormalizeStats)
    assert_type(await source.status(session), dict[str, StreamStatus])
"""
    )
    negative = tmp_path / "invalid_provider.py"
    negative.write_text(
        """from app.core.ingestion_stats import NormalizeStats
from app.services.providers.ingestion import GoogleSource

class Incompatible(GoogleSource):
    async def normalize(self, start: str, end: str) -> NormalizeStats:
        return NormalizeStats()
"""
    )
    # Files outside the project do not inherit its import search path. Resolve the
    # source tree and dependencies explicitly, even without an activated virtualenv.
    config = tmp_path / "pyrightconfig.json"
    config.write_text(
        json.dumps(
            {"extraPaths": [str(ROOT)], "pythonVersion": "3.12", "typeCheckingMode": "standard"}
        )
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pyright",
            "--outputjson",
            "--project",
            str(config),
            "--pythonpath",
            sys.executable,
            str(positive),
            str(negative),
        ],
        cwd=tmp_path,
        env={
            key: value
            for key, value in os.environ.items()
            if key not in {"VIRTUAL_ENV", "PYTHONPATH"}
        },
        text=True,
        capture_output=True,
        timeout=50,
        check=False,
    )
    report = json.loads(result.stdout)
    errors = [item for item in report["generalDiagnostics"] if item["severity"] == "error"]
    assert result.returncode == 1, result.stdout + result.stderr
    assert errors and all(Path(item["file"]) == negative for item in errors), errors
    assert any(item.get("rule") == "reportIncompatibleMethodOverride" for item in errors)


@pytest.mark.parametrize("failure", [RuntimeError("synthetic failure"), CancelledError()])
async def test_shared_pipeline_preserves_failed_page_before_propagating(failure):
    from datetime import UTC, datetime
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.core.fetch_window import FetchWindow
    from app.services.providers.ingestion_contracts import ingest_pages

    page = SimpleNamespace(data_points=[{}], next_page_token=None, cyclic=False)

    class Source:
        keep_after = AsyncMock()

        async def fetch(self, stream, window, **kwargs):
            yield page

    source = Source()
    commit = AsyncMock(side_effect=failure)
    stream = SimpleNamespace(name="third-provider")
    window = FetchWindow(datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC))
    with pytest.raises(type(failure)):
        await anext(ingest_pages(source, stream, window, commit))
    commit.assert_awaited_once_with(page)
    source.keep_after.assert_awaited_once_with(page, failure)


async def test_third_source_uses_the_same_pipeline_without_google_pages():
    from dataclasses import dataclass
    from datetime import UTC, datetime

    from app.core.fetch_window import FetchWindow
    from app.core.ingestion_stats import NormalizeStats, StoreStats
    from app.core.records import RecordType
    from app.services.providers.ingestion_contracts import IngestionSource, ingest_pages
    from app.services.readers.reading import ArchiveReaders

    @dataclass(frozen=True)
    class ThirdPage:
        data_points: list[object]
        next_page_token: str | None = None
        cyclic: bool = False

    class Source:
        name = "third"
        readers = ArchiveReaders("third")

        def __init__(self):
            self.stored = []

        async def fetch(self, stream, window, **kwargs):
            yield ThirdPage([{"synthetic": 1}], "second")
            yield ThirdPage([{"synthetic": 2}])

        async def store_raw(self, session, stream, page, **kwargs):
            self.stored.extend(page.data_points)
            return StoreStats(inserted=len(page.data_points))

        async def keep_after(self, page, failure):
            pytest.fail("No failed write")

        async def normalize(self, window, *, seen_since=None):
            return NormalizeStats(inserted=len(self.stored))

        async def status(self, session):
            return {}

    source = Source()
    assert isinstance(source, IngestionSource)
    stream = RecordType("synthetic")
    window = FetchWindow(datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC))

    async def commit(page):
        return await source.store_raw(None, stream, page)

    progress = [stats.inserted async for _, stats in ingest_pages(source, stream, window, commit)]
    assert progress == [1, 1] and source.stored == [{"synthetic": 1}, {"synthetic": 2}]
    assert (await source.normalize(window)).inserted == 2
    assert await source.status(None) == {}
