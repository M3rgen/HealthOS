"""Coverage test helpers: a small archive, coverage queries and the retrieval-coverage API."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from types import SimpleNamespace

import httpx
import pytest
from coverage_records import source_archive as source_archive
from retrieval_records import END, START, Client, Runner

from app import main
from app.core.config import Settings
from app.db.session import get_session
from app.google_health.client import Page
from app.models.state import RawHealthData

BAND = {
    "platform": "FITBIT",
    "recordingMethod": "DERIVED",
    "device": {"displayName": "Example Band", "formFactor": "FITNESS_BAND"},
}


SEEN = datetime(2026, 10, 1, tzinfo=UTC)


JSON_KEYS = {
    "steps": "steps",
    "sleep": "sleep",
    "weight": "weight",
    "body-fat": "bodyFat",
    "daily-resting-heart-rate": "dailyRestingHeartRate",
    "daily-heart-rate-variability": "dailyHeartRateVariability",
    "daily-oxygen-saturation": "dailyOxygenSaturation",
}


def at(day: date, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


def raw(kind, gid, start, payload, source=BAND, end=None, **overrides):
    values = {
        "id": uuid.uuid4(),
        "data_type": kind,
        "google_data_point_id": gid,
        "source_start_time": start,
        "source_end_time": end,
        "raw_json": {"dataSource": source, JSON_KEYS[kind]: payload},
        "payload_hash": "test",
        "first_seen_at": SEEN,
        "last_seen_at": SEEN,
    }
    return RawHealthData(**(values | overrides))


def daily(kind, gid, day, payload, source=BAND, **flags):
    return raw(kind, gid, at(day), {"date": day.isoformat(), **payload}, source, **flags)


def coverage_of(card, key):
    return next(metric for metric in card["metrics"] if metric["metric_key"] == key)


@pytest.fixture
def archive(sessions, monkeypatch):

    async def add(*rows):
        async with sessions() as session:
            session.add_all(rows)
            await session.commit()

    return add


def query(start, end, **extra):
    return {"action": "coverage", "start_date": str(start), "end_date": str(end), **extra}


QUERY = {
    "start_date": "2026-09-01",
    "end_date": "2026-09-01",
    "timezone": "UTC",
    "data_type": "heart-rate",
}


PATH = "/api/sync/coverage"


AUTH = {"Authorization": "Bearer synthetic-coverage-test-authentication"}


@pytest.fixture
async def api(sessions, monkeypatch):
    settings = Settings(_env_file=None, app_env="test", app_ui_token=AUTH["Authorization"][7:])
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "build_runtime", lambda *_: SimpleNamespace())
    app = main.create_app()

    async def session():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://localhost", headers=AUTH
    ) as client:
        yield client


async def fetch(sessions, start=START, end=END, kind="heart-rate", points=()):
    await Runner(sessions, "sync", kind, start, end).run(Client(Page(list(points), None)))
