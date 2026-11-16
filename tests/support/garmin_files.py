"""Generated original-file HTTP responses; no recorded files or location data."""

import io
import zipfile
from datetime import UTC, datetime, timedelta

import httpx
from fit_factory import fit_file
from garmin_records import FakeGarmin, activity, store_page

from app.core.config import Settings
from app.garmin.client import GarminClient
from support.garmin import unlocked

NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)
PREFIX = "/download-service/files/activity/"
LIST = "/activitylist-service/activities/search/activities"


def zipped(content=None, name="synthetic.fit", *, entries=1):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for index in range(entries):
            archive.writestr(name if index == 0 else f"other-{index}.fit", content or fit_file(b""))
    return stream.getvalue()


class Clock:
    def __init__(self):
        self.now, self.waits = NOW, []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += timedelta(seconds=seconds)


class FileServer(FakeGarmin):
    def __call__(self, request):
        if request.url.path.startswith(PREFIX):
            self.requests.append(request)
            queue = self.statuses.get(request.url.path)
            status = queue.pop(0) if queue else 200
            return httpx.Response(
                status,
                content=self.bodies.get(request.url.path, zipped()),
                # Deliberately wrong: identification must depend on bytes.
                headers={"content-type": "text/plain", **self.headers.get(request.url.path, {})},
            )
        return super().__call__(request)

    def downloads(self):
        return [r for r in self.requests if r.url.path.startswith(PREFIX)]


def pull_service(sessions, tmp_path, *, server=None, clock=None, **overrides):
    server, clock = server or FileServer(), clock or Clock()
    key = tmp_path / "synthetic-key"
    key.write_text("synthetic-personal-key")
    settings = Settings(
        _env_file=None, healthos_sources="garmin", garmin_api_key_file=key, **overrides
    )
    client = GarminClient(
        key, http=httpx.AsyncClient(transport=server.transport()), clock=clock, sleep=clock.sleep
    )
    pull = unlocked(settings, sessions, client=client, clock=clock)
    return pull, server, clock


async def seed_activities(sessions, count=3):
    await store_page(
        sessions, "activities", [activity(i, NOW.date()) for i in range(1, count + 1)], NOW
    )
