"""GET /api/garmin/status and /api/garmin/daily, with and without the integration enabled."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from garmin_records import FakeGarmin, daily_steps, sleep, store_page
from sqlalchemy import event
from support.postgres_reads import only_reads, read_statements
from test_garmin_pull import NOW, STEPS_4_DAYS, TODAY, Clock, service
from test_garmin_step_validity import insert_raw_json

from app import main
from app.core.config import Settings, get_settings
from app.main import create_app
from app.models.garmin import GarminRawRecord
from app.services.identities.source_precedence import Policy


@pytest.fixture
async def client(request, monkeypatch):
    settings = Settings(_env_file=None, healthos_sources=getattr(request, "param", "google"))
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    application = create_app()
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as http:
        yield http, application
    await application.state.runtime.shutdown()


@pytest.mark.parametrize("client", ["garmin"], indirect=True)
@pytest.mark.parametrize("token", ["-1e-1000", "-1e-10000"])
async def test_daily_api_rejects_underflowed_negative_json_numbers(sessions, client, token):
    from test_garmin_normalize import DAY

    http, application = client
    application.state.runtime.garmin.sessions = sessions
    async with sessions.begin() as session:
        for kind, body in (
            ("daily-summaries", '{"heartRate":{"restingValue":TOKEN},"stress":{"avgLevel":20}}'),
            ("heart-rate", '{"restingHeartRate":60}'),
            ("spo2", '{"averageSpO2":TOKEN}'),
            ("steps", '{"totalSteps":1000000000000000000000000000000,"totalDistance":12.25}'),
        ):
            await insert_raw_json(session, kind, body.replace("TOKEN", token))
    response = await http.get("/api/garmin/daily", params={"from": str(DAY), "to": str(DAY)})
    assert response.status_code == 200
    day = response.json()["days"][0]
    assert day["resting_heart_rate"] is None and day["spo2_average"] is None
    assert day["stress_average"] == 20 and type(day["stress_average"]) is int
    assert day["steps"] == 10**30 and type(day["steps"]) is int
    assert day["distance_m"] == 12.25 and type(day["distance_m"]) is float


async def test_disabled_integration_routes_are_absent(client):
    http, application = client
    assert application.state.runtime.garmin is None
    assert (await http.get("/api/garmin/status")).status_code == 404
    daily = await http.get("/api/garmin/daily", params={"from": "2026-11-01", "to": "2026-11-07"})
    assert daily.status_code == 404


@pytest.mark.parametrize("client", ["garmin"], indirect=True)
async def test_daily_rejects_an_empty_or_too_long_range(client):
    http, _ = client
    too_long = await http.get(
        "/api/garmin/daily", params={"from": "2026-01-01", "to": "2026-06-01"}
    )
    assert too_long.status_code == 422
    assert too_long.json()["detail"] == {"code": "garmin_invalid_range", "max_days": 92}
    reversed_range = await http.get(
        "/api/garmin/daily", params={"from": "2026-11-07", "to": "2026-11-01"}
    )
    assert reversed_range.status_code == 422
    missing = await http.get("/api/garmin/daily")
    assert missing.status_code == 422


async def test_enabled_integration_reads_the_archive(sessions, monkeypatch, tmp_path):
    key = tmp_path / "key"
    key.write_text("synthetic")
    monkeypatch.setenv("GARMIN_ENABLED", "true")
    monkeypatch.setenv("GARMIN_API_KEY_FILE", str(key))
    get_settings.cache_clear()
    try:
        application = create_app()
    finally:
        get_settings.cache_clear()
    runtime = application.state.runtime
    assert runtime.garmin is not None
    runtime.garmin.sessions = sessions
    async with sessions.begin() as session:
        session.add(
            GarminRawRecord(
                kind="steps",
                key="2026-11-06",
                body={"calendarDate": "2026-11-06", "totalSteps": 5120, "totalDistance": 3800},
                body_sha256="a" * 64,
                first_seen_at=datetime(2026, 11, 7, tzinfo=UTC),
            )
        )
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as http:
        status = await http.get("/api/garmin/status")
        daily = await http.get(
            "/api/garmin/daily", params={"from": "2026-11-06", "to": "2026-11-06"}
        )
    await runtime.shutdown()
    body = status.json()
    assert body["enabled"] and body["key_file_present"] and body["raw_records"] == 1
    assert body["kinds"]["steps"]["newest_key"] == "2026-11-06"
    assert "synthetic" not in status.text  # the key never appears
    day = daily.json()["days"][0]
    assert day["steps"] == 5120 and day["distance_m"] == 3800 and day["sleep_seconds"] is None


async def test_disabled_garmin_routes_are_not_registered():
    application = create_app()
    paths = application.openapi()["paths"]
    await application.state.runtime.shutdown()
    assert not {"/api/garmin/status", "/api/garmin/daily"} & set(paths)


@pytest.mark.parametrize("client", ["garmin"], indirect=True)
async def test_daily_api_returns_the_last_observed_content_after_reversion(
    sessions, client, tmp_path
):
    http, application = client
    fake = FakeGarmin()
    key = tmp_path / "key"
    key.write_text("synthetic")
    clock = Clock()
    pull = service(sessions, fake, key, clock)
    await application.state.runtime.garmin.aclose()
    application.state.runtime.garmin = pull
    for value in (100, -1, -0.0001, -1e-300, 0, -0.0, 200, 100, 100):
        fake.bodies[STEPS_4_DAYS] = [daily_steps(TODAY, steps=value)]
        summary = await pull.run(["steps"])
        assert not summary.failed
        response = await http.get(
            "/api/garmin/daily", params={"from": str(TODAY), "to": str(TODAY)}
        )
        assert response.status_code == 200
        assert response.json()["days"][0]["steps"] == (None if value < 0 else value)


@pytest.mark.parametrize("client", ["garmin"], indirect=True)
@pytest.mark.parametrize(
    "kind,body,expected",
    [
        ("heart-rate", {"restingHeartRate": 0}, {"resting_heart_rate": 0}),
        ("stress", {"values": {"overallStressLevel": 0}}, {"stress_average": 0}),
        ("hrv", {"hrvSummary": {"lastNightAvg": 0}}, {"hrv_last_night_average": 0}),
        (
            "sleep-summaries",
            sleep(TODAY),
            {
                "sleep_seconds": sleep(TODAY)["sleepTimeSeconds"],
                "sleep_score": sleep(TODAY)["sleepScores"]["overall"]["value"],
                "sleep_stages_seconds": {
                    name: sleep(TODAY)[field]
                    for name, field in (
                        ("deep", "deepSleepSeconds"),
                        ("light", "lightSleepSeconds"),
                        ("rem", "remSleepSeconds"),
                        ("awake", "awakeSleepSeconds"),
                    )
                },
            },
        ),
    ],
)
async def test_alternate_only_daily_is_read_only_and_tracks_reversions(
    sessions, client, kind, body, expected
):
    http, application = client
    application.state.runtime.garmin.sessions = sessions
    engine = sessions.kw["bind"].sync_engine

    for index, content in enumerate((body, {}, body, body)):
        record = {**content, "calendarDate": str(TODAY)}
        await store_page(sessions, kind, [record], NOW + timedelta(minutes=index))
        event.listen(engine, "before_cursor_execute", only_reads)
        try:
            response = await http.get(
                "/api/garmin/daily",
                params={
                    "from": str(TODAY - timedelta(days=91)),
                    "to": str(TODAY),
                },
            )
        finally:
            event.remove(engine, "before_cursor_execute", only_reads)
        assert response.status_code == 200
        result = response.json()
        assert len(result["days"]) == 1 and result["days"][0]["date"] == str(TODAY)
        for metric, value in expected.items():
            actual = result["days"][0][metric]
            if index == 1:
                value = dict.fromkeys(value) if isinstance(value, dict) else None
            assert actual == value
        assert result["source"] == "garmin" and result["policy"] == Policy().as_dict()
        assert result["precedence"]["sleep_seconds"] == "secondary"


@pytest.mark.parametrize("client", ["garmin"], indirect=True)
@pytest.mark.parametrize("resting_kind", ["heart-rate", "daily-summaries"])
@pytest.mark.parametrize("value", [-1, -0.0001, -1e-300, "-1e-10000", 0, -0.0, 12.25, "12.25"])
async def test_daily_scalar_api_reads_archived_values_without_accepting_numeric_text(
    sessions, client, resting_kind, value
):
    from test_garmin_scalar_validity import only_select, snapshot

    from app.services.providers.garmin_normalize import normalize_window

    http, application = client
    application.state.runtime.garmin.sessions = sessions
    for kind, body in (
        ("heart-rate", {"restingHeartRate": value if resting_kind == "heart-rate" else 60}),
        ("daily-summaries", {"heartRate": {"restingValue": value}}),
        ("hrv-summaries", {"lastNightAvg": value}),
        ("spo2", {"averageSpO2": value}),
    ):
        if kind == "daily-summaries" and resting_kind != kind:
            continue
        await store_page(sessions, kind, [{"calendarDate": str(TODAY), **body}], NOW)
    async with sessions.begin() as session:
        await normalize_window(session, TODAY, TODAY)
    engine = sessions.kw["bind"].sync_engine
    async with sessions() as session:
        before = await snapshot(session)
        event.listen(engine, "before_cursor_execute", only_select)
        try:
            for _ in range(2):
                response = await http.get(
                    "/api/garmin/daily", params={"from": str(TODAY), "to": str(TODAY)}
                )
                assert response.status_code == 200
                day = response.json()["days"][0]
                expected = value if isinstance(value, int | float) and value >= 0 else None
                for field in ("resting_heart_rate", "hrv_last_night_average", "spo2_average"):
                    selected = (
                        60
                        if field == "resting_heart_rate"
                        and resting_kind == "daily-summaries"
                        and isinstance(value, str)
                        else expected
                    )
                    assert day[field] == selected
                    assert type(day[field]) is type(selected)
        finally:
            event.remove(engine, "before_cursor_execute", only_select)
        assert await snapshot(session) == before


@pytest.mark.parametrize("client", ["garmin"], indirect=True)
async def test_sleep_api_preserves_other_fields_and_never_rescues_negative_primary(
    sessions, client
):
    http, application = client
    application.state.runtime.garmin.sessions = sessions
    for kind, body in (
        ("sleep-summaries", sleep(TODAY)),
        (
            "sleep",
            {
                **sleep(TODAY),
                "sleepTimeSeconds": -0.0001,
                "deepSleepSeconds": -1,
                "lightSleepSeconds": "18000",
                "remSleepSeconds": 0,
                "awakeSleepSeconds": -1e-300,
            },
        ),
    ):
        await store_page(sessions, kind, [body], NOW)
    response = await http.get("/api/garmin/daily", params={"from": str(TODAY), "to": str(TODAY)})
    assert response.status_code == 200
    day = response.json()["days"][0]
    assert day["sleep_seconds"] is None
    assert day["sleep_stages_seconds"] == {"deep": None, "light": None, "rem": 0, "awake": None}
    assert type(day["sleep_stages_seconds"]["rem"]) is int
    assert day["sleep_score"] == 80


@pytest.mark.parametrize("client", ["garmin"], indirect=True)
async def test_daily_projects_pending_raw_observations_without_loading_bodies(
    sessions, client, monkeypatch
):
    from test_garmin_scalar_validity import snapshot

    from app.services.providers import garmin_current
    from app.services.providers.garmin_normalize import normalize_window

    http, application = client
    application.state.runtime.garmin.sessions = sessions
    await store_page(sessions, "steps", [daily_steps(TODAY, steps=100)], NOW)
    async with sessions.begin() as session:
        await normalize_window(session, TODAY, TODAY)
    body = {
        **daily_steps(TODAY, steps=250),
        "heartRateValues": [[i, 70] for i in range(10_000)],
        "stressValuesArray": [[i, 20] for i in range(10_000)],
        "metadata": {"unread": "x" * 100_000},
    }
    await store_page(sessions, "steps", [body], NOW + timedelta(minutes=1))
    async with sessions() as session:
        before = await snapshot(session)
    engine, statements = sessions.kw["bind"].sync_engine, []

    def only_select(conn, cursor, statement, parameters, context, executemany):
        only_reads(conn, cursor, statement, parameters, context, executemany)
        statements.append(statement)

    async def no_full_records(*args, **kwargs):
        pytest.fail("daily must use its SQL projection, not the normalization body reader")

    def no_json_body(*args):
        pytest.fail("daily must not decode a full JSON body")

    with monkeypatch.context() as patch:
        patch.setattr(garmin_current, "newest_records", no_full_records)
        patch.setattr(GarminRawRecord.body.type, "_cached_result_processor", no_json_body)
        event.listen(engine, "before_cursor_execute", only_select)
        try:
            response = await http.get(
                "/api/garmin/daily", params={"from": str(TODAY), "to": str(TODAY)}
            )
        finally:
            event.remove(engine, "before_cursor_execute", only_select)
    assert response.status_code == 200 and len(read_statements(statements)) == 1
    result = response.json()
    assert result["days"][0]["steps"] == 250
    assert result["source"] == "garmin" and result["policy"] == Policy().as_dict()
    assert result["precedence"]["steps"] == "secondary"
    async with sessions() as session:
        assert await snapshot(session) == before
