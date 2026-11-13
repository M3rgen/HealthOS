"""Synthetic Garmin Personal API payloads, shaped like the live responses, with made-up values.

Field names follow the endpoints' real responses (checked against a live account on
2026-11-07); every value here is invented.
Shared by the client, pull, API and CLI tests. Values deliberately stay round and patterned;
never copy values from a real account.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

import httpx

from app.core.config import Settings
from app.garmin.client import TOKEN_PATH, GarminExchange
from app.services.providers.garmin_ingestion import GarminPage, GarminSource
from app.services.providers.garmin_kinds import BY_NAME

# A JWT-shaped token: header.payload.signature, payload with ``exp`` far in the future.
TOKEN = (
    "eyJhbGciOiJSUzI1NiJ9."
    "eyJleHAiOjQxMDI0NDQ4MDAsImlhdCI6NDEwMjM1ODQwMCwic2NvcGUiOiJDT05ORUNUX1JFQUQifQ."
    "c2ln"
)


def daily_summary(day: date, *, steps: int = 7000, resting: int = 50) -> dict[str, Any]:
    return {
        "calendarDate": day.isoformat(),
        "uuid": f"00000000-0000-4000-8000-{day.strftime('%Y%m%d'):0>12}",
        "userProfilePk": 1,
        "source": "GARMIN",
        "includesWellnessData": True,
        "includesActivityData": False,
        "heartRate": {"restingValue": resting, "minValue": 40, "maxValue": 120},
        "bodyBattery": {
            "chargedValue": 60,
            "drainedValue": 50,
            "latestValue": 40,
            "maxValue": 90,
            "minValue": 30,
            "featureVersion": "2",
        },
        "stress": {"avgLevel": 20, "maxLevel": 80, "qualifier": "BALANCED"},
        "movement": {"steps": {"total": steps, "goal": 10000}},
        "calories": {"burnedTotal": 2000, "burnedActive": 400, "burnedResting": 1600},
        "respiration": {"avgValue": 20, "minValue": 10, "maxValue": 30},
    }


def daily_steps(day: date, *, steps: int = 7000) -> dict[str, Any]:
    return {
        "calendarDate": day.isoformat(),
        "totalSteps": steps,
        "stepGoal": 10000,
        "totalDistance": steps * 0.5,
    }


def daily_stress(day: date) -> dict[str, Any]:
    return {
        "calendarDate": day.isoformat(),
        "values": {
            "overallStressLevel": 20,
            "restStressDuration": 30000,
            "lowStressDuration": 15000,
            "mediumStressDuration": 5000,
            "highStressDuration": 1000,
        },
    }


def heart_rate(day: date) -> dict[str, Any]:
    return {
        "calendarDate": day.isoformat(),
        "userProfilePK": 1,
        "startTimestampGMT": f"{day.isoformat()}T00:00:00.0",
        "endTimestampGMT": f"{day.isoformat()}T23:59:59.0",
        "restingHeartRate": 50,
        "minHeartRate": 40,
        "maxHeartRate": 120,
        "lastSevenDaysAvgRestingHeartRate": 50,
        "heartRateValueDescriptors": [
            {"key": "timestamp", "index": 0},
            {"key": "heartrate", "index": 1},
        ],
        "heartRateValues": [[1_700_000_000_000, 60], [1_700_000_120_000, 70]],
    }


def sleep(day: date, *, seconds: int = 28_800) -> dict[str, Any]:
    return {
        "id": 900_000 + day.toordinal(),
        "userProfilePK": 1,
        "calendarDate": day.isoformat(),
        "sleepTimeSeconds": seconds,
        "napTimeSeconds": 0,
        "deepSleepSeconds": 6_000,
        "lightSleepSeconds": 18_000,
        "remSleepSeconds": 3_600,
        "awakeSleepSeconds": 1_200,
        "unmeasurableSleepSeconds": 0,
        "sleepStartTimestampGMT": 1_700_000_000_000,
        "sleepEndTimestampGMT": 1_700_000_000_000 + seconds * 1000,
        "sleepFromDevice": True,
        "retro": False,
        "sleepScores": {"overall": {"value": 80, "qualifierKey": "GOOD"}},
        "averageRespirationValue": 20.0,
        "avgHeartRate": 60.0,
    }


def hrv_summary(day: date) -> dict[str, Any]:
    return {
        "calendarDate": day.isoformat(),
        "weeklyAvg": None,
        "lastNightAvg": 40,
        "lastNight5MinHigh": 60,
        "baseline": None,
        "status": "UNBALANCED",
        "feedbackPhrase": "HRV_UNBALANCED_LOW_1",
        "createTimeStamp": f"{day.isoformat()}T06:00:00.0",
    }


def activity(activity_id: int, day: date) -> dict[str, Any]:
    return {
        "activityId": activity_id,
        "activityName": "Yürüyüş",
        "startTimeLocal": f"{day.isoformat()} 18:00:00",
        "startTimeGMT": f"{day.isoformat()} 17:00:00",
        "activityType": {"typeId": 9, "typeKey": "walking", "parentTypeId": 17},
        "distance": 3000.0,
        "duration": 2400.0,
        "averageHR": 100.0,
        "maxHR": 120.0,
        "calories": 200.0,
        "manualActivity": False,
    }


class FakeGarmin:
    """An httpx.MockTransport handler answering like the Personal API, with a request log.

    ``bodies`` maps a path (without query) to a JSON body or a callable of the request.
    ``statuses`` forces a status for the first n calls of a path (``[429, 200]``).
    """

    def __init__(self) -> None:
        self.bodies: dict[str, Any] = {}
        self.statuses: dict[str, list[int]] = {}
        self.requests: list[httpx.Request] = []
        self.token_requests = 0
        self.token = TOKEN
        self.token_status = 200
        self.headers: dict[str, dict[str, str]] = {}

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == TOKEN_PATH:
            self.token_requests += 1
            assert request.method == "POST"
            if self.token_status != 200:
                return httpx.Response(self.token_status)
            return httpx.Response(200, text=self.token, headers={"content-type": "text/plain"})
        assert request.method == "GET"
        queue = self.statuses.get(path)
        status = queue.pop(0) if queue else 200
        if status == 401:
            return httpx.Response(401, headers={"www-authenticate": 'Bearer error="invalid_token"'})
        if status == 204:
            return httpx.Response(204)
        if status != 200:
            return httpx.Response(status, headers=self.headers.get(path, {}), json={})
        body = self.bodies.get(path, [])
        if isinstance(body, Callable):
            body = body(request)
        return httpx.Response(
            200, content=json.dumps(body).encode(), headers={"content-type": "application/json"}
        )

    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]


def record_page(kind: str, records: list, now: datetime) -> GarminPage:
    """A synthetic successful response carried through the real page writer."""
    exchange = GarminExchange(
        fetched_at=now,
        kind=kind,
        context_id=None,
        method="GET",
        path="/synthetic/records",
        query=None,
        attempt=0,
        status_code=200,
        headers={},
        body=json.dumps(records).encode(),
    )
    fallback = records[0]["calendarDate"] if records and BY_NAME[kind].mode == "day" else None
    return GarminPage(records, None, [exchange], fallback)


async def store_page(sessions, kind: str, records: list, now: datetime):
    source = GarminSource(Settings(_env_file=None), sessions, clock=lambda: now)
    async with sessions.begin() as session:
        return await source.store_raw(session, BY_NAME[kind], record_page(kind, records, now))
