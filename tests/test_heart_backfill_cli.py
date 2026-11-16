"""The fill command: free-space gate, numbers-only progress, clean stop on a signal."""

import asyncio
import os
import signal
from contextlib import asynccontextmanager

import pytest

import app.db.session
from app.cli import heart_backfill
from app.services.readers.heart_fill import FillProgress


@pytest.fixture
def archive(monkeypatch):
    """No database: the command's collaborators replaced by observable fakes."""
    calls = {}

    @asynccontextmanager
    async def lifespan(settings, factory):
        yield "sessions"

    async def space(sessions):
        return calls.get("space", (10, 20))

    async def ready(sessions):
        return calls.get("ready", True)

    monkeypatch.setattr(app.db.session, "database_lifespan", lifespan)
    monkeypatch.setattr(heart_backfill, "_space", space)
    monkeypatch.setattr(heart_backfill, "_ready", ready)
    monkeypatch.setattr(heart_backfill, "REPORT_EVERY", 0.01)
    monkeypatch.setattr(heart_backfill, "GRACE", 0.05)
    return calls


def fake(monkeypatch, body):
    monkeypatch.setattr(heart_backfill, "backfill_heart_hours", body)


def test_hours_must_be_positive_and_default_to_the_whole_horizon(archive, monkeypatch):
    with pytest.raises(SystemExit):
        heart_backfill.main(["--hours", "0"])
    seen = []

    async def fill(sessions, *, limit, progress, halt):
        seen.append(limit)
        progress.packets = 3
        return 3

    fake(monkeypatch, fill)
    assert heart_backfill.main([]) == 0
    assert heart_backfill.main(["--hours", "5"]) == 0
    assert seen == [None, 5]


def test_too_little_temporary_space_starts_nothing(archive, monkeypatch, capsys):
    archive["space"] = (16 * 1024**3, 15 * 1024**3)
    fake(monkeypatch, pytest.fail)
    assert heart_backfill.main([]) == 1
    assert "gereken 16.0 GB, boş 15.0 GB" in capsys.readouterr().out


@pytest.mark.parametrize("stubborn", [False, True])
def test_a_signal_stops_cleanly_and_reports_numbers(archive, monkeypatch, capsys, stubborn):
    archive["ready"] = False

    async def fill(sessions, *, limit, progress, halt):
        progress.hours, progress.packets = 7, 6
        asyncio.get_running_loop().call_later(0.05, os.kill, os.getpid(), signal.SIGTERM)
        await (asyncio.sleep(3600) if stubborn else halt.wait())  # stubborn: cancelled
        return 6

    fake(monkeypatch, fill)
    assert heart_backfill.main([]) == heart_backfill.INTERRUPTED
    out = capsys.readouterr().out
    assert "İşlenen saat: 7, yazılan paket: 6, süre:" in out
    assert "Durduruldu; aynı komut kaldığı yerden sürer." in out
    assert "Traceback" not in out


def test_progress_line_has_only_counts():
    progress = FillProgress(hours=2, packets=1)
    assert progress.line().startswith("İşlenen saat: 2, yazılan paket: 1, süre: ")
