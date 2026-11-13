"""A permanent archive without backups must say so on /health, without changing its status."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import health
from app.core.config import Settings
from app.runtime import build_runtime
from app.services.operations.backup_status import backup_reasons, read_backup_status

ATTEMPT = "2026-10-16T03:30:00Z"
EARLIER = "2026-10-15T03:30:00Z"


def status_file(tmp_path: Path, result: str, success: str | None) -> Path:
    path = tmp_path / "status.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "last_attempt": {"result": result, "timestamp": ATTEMPT},
                "last_success_at": success,
            }
        ),
        encoding="utf-8",
    )
    return path


def reports_for(sessions, *paths: Path) -> list[dict]:
    """One client for all paths: the PostgreSQL pool stays on the event loop that opened it."""
    config = Settings(_env_file=None, app_env="test", backup_status_file=paths[0])
    app = FastAPI()
    app.state.settings, app.state.runtime = config, build_runtime(config, sessions)
    app.include_router(health.router)
    reports = []
    with TestClient(app) as client:
        for path in paths:
            app.state.settings = config.model_copy(update={"backup_status_file": path})
            response = client.get("/health")
            assert response.status_code == 200, response.text
            reports.append(response.json())
    return reports


def report_for(sessions, path: Path) -> dict:
    return reports_for(sessions, path)[0]


def test_a_missing_backup_is_a_reason_and_leaves_the_status_alone(sessions, tmp_path):
    report, with_backup = reports_for(
        sessions, tmp_path / "absent.json", status_file(tmp_path, "success", ATTEMPT)
    )

    assert "backup_missing" in report["reasons"]
    assert report["last_backup_success"] is None
    # What monitors and the interface read must not depend on the backup state.
    assert report["status"] == with_backup["status"]
    assert "backup_missing" not in with_backup["reasons"]
    assert with_backup["last_backup_success"] == ATTEMPT


def test_a_failed_attempt_names_itself_and_keeps_the_last_success(sessions, tmp_path):
    report = report_for(sessions, status_file(tmp_path, "failure", EARLIER))
    assert "backup_failed" in report["reasons"]
    assert report["last_backup_success"] == EARLIER


@pytest.mark.parametrize(
    ("body", "expected"),
    [("not json", ["backup_status_unreadable"])],
)
def test_damaged_metadata_is_not_reported_as_no_backup(tmp_path, body, expected):
    path = tmp_path / "status.json"
    path.write_text(body, encoding="utf-8")
    assert backup_reasons(read_backup_status(path)) == expected


def test_an_install_that_does_not_track_backups_has_no_reason():
    assert backup_reasons(read_backup_status(None)) == []


def test_the_timestamp_format_matches_the_backup_route(tmp_path):
    state = read_backup_status(status_file(tmp_path, "success", ATTEMPT))
    assert state["last_success_at"] == datetime(
        2026, 10, 16, 3, 30, tzinfo=UTC
    ).isoformat().replace("+00:00", "Z")
