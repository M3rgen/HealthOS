"""PostgreSQL: the Garmin bearer token never reaches ``garmin_api_responses``, and the archive's
append-only triggers still refuse to change or remove a token-exchange row."""

import hashlib
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from garmin_records import TOKEN, daily_steps
from garmin_token_leaks import SecretEchoGarmin, archived, leaks
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.config import Settings
from app.garmin.client import GarminClient

pytestmark = pytest.mark.integration

KEY = "synthetic-personal-api-key-0123456789"
PLACEHOLDER = b"[redacted: garmin bearer token]"
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
TODAY = date(2026, 11, 7)
DAYS = [TODAY - timedelta(days=offset) for offset in range(3, -1, -1)]
STEPS = f"/usersummary-service/stats/steps/daily/{DAYS[0].isoformat()}/{DAYS[-1].isoformat()}"


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


async def test_pg_archive_keeps_no_token_and_stays_append_only(
    pg_sessions, tmp_path, unlocked_pull
):
    key_file = tmp_path / "connect-personal-api-key"
    key_file.write_text(KEY)
    fake = SecretEchoGarmin()
    fake.bodies[STEPS] = [daily_steps(day) for day in DAYS]
    fake.token_headers = {"set-cookie": f"session={TOKEN}"}
    # A retry, a 401 echoing the bearer (renewal), then a success.
    fake.statuses[STEPS] = [503, 401]
    fake.echo_401 = {STEPS}
    clock = Clock()
    client = GarminClient(
        key_file, http=httpx.AsyncClient(transport=fake.transport()), sleep=clock.sleep, clock=clock
    )
    settings = Settings(_env_file=None, garmin_enabled=True, garmin_api_key_file=key_file)
    pull = unlocked_pull(settings, pg_sessions, client=client, clock=clock)
    summary = await pull.run(["steps"])
    assert not summary.failed, summary.as_dict()
    assert summary.kinds["steps"].new_records == 4

    rows = await archived(pg_sessions)
    assert [(row["kind"], row["status_code"]) for row in rows] == [
        ("token", 200),
        ("steps", 503),
        ("steps", 401),
        ("token", 200),
        ("steps", 200),
    ]
    assert await leaks(pg_sessions, TOKEN, KEY) == []
    token_rows = [row for row in rows if row["kind"] == "token"]
    assert all(bytes(row["body"]) == PLACEHOLDER for row in token_rows)
    assert all(
        row["body_sha256"] == hashlib.sha256(TOKEN.encode()).hexdigest()
        and row["body_size"] == len(TOKEN)
        for row in token_rows
    )
    data = rows[-1]
    assert hashlib.sha256(bytes(data["body"])).hexdigest() == data["body_sha256"]

    token_id = token_rows[0]["id"]
    for statement in (
        f"UPDATE garmin_api_responses SET body = '\\x00'::bytea WHERE id = {token_id}",
        f"DELETE FROM garmin_api_responses WHERE id = {token_id}",
        "TRUNCATE garmin_api_responses CASCADE",
    ):
        async with pg_sessions() as session:
            with pytest.raises(DBAPIError, match="refused"):
                await session.execute(text(statement))
    assert len(await archived(pg_sessions)) == len(rows)
    await pull.aclose()
