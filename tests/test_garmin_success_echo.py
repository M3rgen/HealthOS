"""Credential-bearing successes never become raw records or successful checkpoints."""

import json
from datetime import timedelta

import httpx
import pytest
from garmin_records import TOKEN, FakeGarmin, daily_steps
from garmin_token_leaks import scan
from test_garmin_pagination import evidence
from test_garmin_pull import TODAY, Clock, service
from test_garmin_token_redaction import KEY, PLACEHOLDER, client, sha
from test_garmin_token_redaction import credential as credential
from test_garmin_token_redaction import echo_body as echo_body
from test_garmin_token_redaction import key_file as key_file

from app.garmin.client import GarminCredentialEchoError, GarminResponseDecodeError
from app.models.garmin import GarminApiResponse, GarminRawRecord, GarminRecordObservation

PATH = f"/usersummary-service/stats/steps/daily/{TODAY}/{TODAY}"
RUN_PATH = f"/usersummary-service/stats/steps/daily/{TODAY - timedelta(days=1)}/{TODAY}"
ERROR = "Garmin response contained a credential"


@pytest.mark.parametrize("backfill", [False, True])
@pytest.mark.parametrize("location", ["key", "value"])
async def test_echo_archives_once_preserves_progress_and_recovers(
    sessions, key_file, echo_body, backfill, location, caplog
):
    fake, clock = FakeGarmin(), Clock()
    fake.bodies[PATH] = fake.bodies[RUN_PATH] = [daily_steps(TODAY)]
    pull = service(sessions, fake, key_file, clock, garmin_lookback_days=1)
    try:
        assert not (await pull.run(["steps"])).failed
        before, success = await evidence(sessions), await pull.status()
        assert len(before[GarminRawRecord]) == len(before[GarminRecordObservation]) == 1
        clock.now += timedelta(minutes=1)
        raw, failing = echo_body(location), True
        path = PATH if backfill else RUN_PATH

        def respond(request):
            if failing and request.url.path == path:
                fake.requests.append(request)
                return httpx.Response(200, content=raw)
            return fake(request)

        await pull.client.http.aclose()
        pull.client.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        fake.requests.clear()
        since, kinds = TODAY - timedelta(days=28), ["steps", "daily-summaries"]
        report = (
            await pull.backfill(since, TODAY, kinds=kinds)
            if backfill
            else (await pull.run(kinds)).as_dict()
        )
        entry = report["kinds"]["steps"]
        assert entry["error"] == ERROR
        assert entry["records"] == entry["new_records"] == 0
        assert fake.paths().count(path) == 1  # A credential echo is not retryable.
        after = await evidence(sessions)
        for model, rows in before.items():
            assert after[model][: len(rows)] == rows
        for model in (GarminRawRecord, GarminRecordObservation):
            assert after[model] == before[model]
        new = after[GarminApiResponse][len(before[GarminApiResponse]) :]
        steps = [row for row in new if row.kind == "steps"]
        assert len(steps) == 1 + int(backfill)
        stopped = steps[-1]
        assert stopped.body == PLACEHOLDER and stopped.status_code == 200 and stopped.complete
        assert (stopped.body_sha256, stopped.body_size, stopped.error) == (sha(raw), len(raw), None)
        assert (
            scan([dict(row._mapping) for rows in after.values() for row in rows], TOKEN, KEY) == []
        )
        status = await pull.status()
        state = status["kinds"]["steps"]
        assert state["last_success_at"] == success["kinds"]["steps"]["last_success_at"]
        assert state["last_error_at"] > state["last_success_at"] and state["last_error"] == ERROR
        assert status["failing_kinds"] == ["steps"]
        assert status["kinds"]["daily-summaries"]["last_success_at"] is not None
        if backfill:
            assert not entry["complete"] and state["backfill"]["next"] == str(TODAY)
        assert (
            scan([report, status, *[vars(record) for record in caplog.records]], TOKEN, KEY) == []
        )
        failing = False
        clock.now += timedelta(minutes=1)
        fake.requests.clear()
        recovered = (
            await pull.backfill(since, TODAY, kinds=kinds)
            if backfill
            else (await pull.run(kinds)).as_dict()
        )
        assert all(entry["error"] is None for entry in recovered["kinds"].values())
        if backfill:
            assert recovered["kinds"]["steps"]["complete"]
            assert recovered["kinds"]["steps"]["resumed_from"] == str(TODAY)
            assert fake.paths() == [PATH]
        kept = await evidence(sessions)
        for model, rows in after.items():
            assert kept[model][: len(rows)] == rows
        assert kept[GarminRawRecord] == before[GarminRawRecord]
        assert len(kept[GarminRecordObservation]) == len(before[GarminRecordObservation]) + 1
        clean = [row for row in kept[GarminApiResponse] if row.kind == "steps"][-1]
        assert clean.body == json.dumps(fake.bodies[path]).encode()
        assert (await pull.status())["kinds"]["steps"]["last_success_at"] > state["last_error_at"]
    finally:
        await pull.aclose()


@pytest.mark.parametrize("depth", [0, 300, 10000])
async def test_deep_escaped_echo_keeps_masked_evidence_without_scanner_recursion(
    key_file, depth, credential
):
    escaped = "".join(f"\\u{ord(char):04x}" for char in credential).encode()
    raw = b"[" * depth + b'"' + escaped + b'"' + b"]" * depth
    if depth == 0:  # The full decoder discards the first duplicate value.
        raw = b'{"debug":' + raw + b',"debug":null}'
    fake = FakeGarmin()
    garmin, exchanges = client(key_file, fake), []

    def respond(request):
        return httpx.Response(200, content=raw) if request.method == "GET" else fake(request)

    await garmin.http.aclose()
    garmin.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        error = GarminResponseDecodeError if depth == 10000 else GarminCredentialEchoError
        with pytest.raises(error) as caught:
            await garmin.get(PATH, kind="steps", exchanges=exchanges)
        assert caught.value.__cause__ is None and caught.value.__context__ is None
        assert exchanges[-1].body == PLACEHOLDER
        assert (exchanges[-1].sha256, exchanges[-1].size) == (sha(raw), len(raw))
    finally:
        await garmin.aclose()


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-32"])
@pytest.mark.parametrize("echo", [False, True])
@pytest.mark.parametrize("padding", [0, 1024 * 1024])
async def test_json_encodings_and_literal_unicode_escape_preserve_clean_bytes(
    key_file, encoding, echo, padding, credential
):
    literal = "\\u" + f"{ord(credential[0]):04x}" + credential[1:]
    payload = {
        "padding": "clean\\u0000" * (padding // 16),
        "debug": credential if echo else literal,
    }
    raw = json.dumps(payload).encode(encoding)
    fake = FakeGarmin()
    garmin, exchanges = client(key_file, fake), []

    def respond(request):
        return httpx.Response(200, content=raw) if request.method == "GET" else fake(request)

    await garmin.http.aclose()
    garmin.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        if echo:
            with pytest.raises(GarminCredentialEchoError):
                await garmin.get(PATH, kind="steps", exchanges=exchanges)
        else:
            result = await garmin.get(PATH, kind="steps", exchanges=exchanges)
            assert result.json == payload
        assert exchanges[-1].body == (PLACEHOLDER if echo else raw)
        assert (exchanges[-1].sha256, exchanges[-1].size) == (sha(raw), len(raw))
    finally:
        await garmin.aclose()
