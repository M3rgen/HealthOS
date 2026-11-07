"""Synthetic Garmin Personal API payloads, shaped like the live responses, with made-up values.

Field names follow the endpoints' real responses (checked against a live account on
2026-11-07); every value here is invented. Shared by the client, pull, API and CLI tests.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date
from typing import Any

import httpx

from app.garmin.client import TOKEN_PATH

# A JWT-shaped token: header.payload.signature, payload with ``exp`` far in the future.
TOKEN = (
    "eyJhbGciOiJSUzI1NiJ9."
    "eyJleHAiOjQxMDI0NDQ4MDAsImlhdCI6NDEwMjM1ODQwMCwic2NvcGUiOiJDT05ORUNUX1JFQUQifQ."
    "c2ln"
)


def daily_summary(day: date, *, steps: int = 7400, resting: int = 52) -> dict[str, Any]:
    return {
        "calendarDate": day.isoformat(),
        "uuid": f"00000000-0000-4000-8000-{day.strftime('%Y%m%d'):0>12}",
        "userProfilePk": 1,
        "source": "GARMIN",
        "includesWellnessData": True,
        "includesActivityData": False,
        "heartRate": {"restingValue": resting, "minValue": 48, "maxValue": 131},
        "bodyBattery": {
            "chargedValue": 61,
            "drainedValue": 54,
            "latestValue": 42,
            "maxValue": 88,
            "minValue": 27,
            "featureVersion": "2",
        },
        "stress": {"avgLevel": 24, "maxLevel": 81, "qualifier": "BALANCED"},
        "movement": {"steps": {"total": steps, "goal": 8000}},
        "calories": {"burnedTotal": 2310, "burnedActive": 420, "burnedResting": 1890},
        "respiration": {"avgValue": 15, "minValue": 11, "maxValue": 21},
    }


def daily_steps(day: date, *, steps: int = 7400) -> dict[str, Any]:
    return {
        "calendarDate": day.isoformat(),
        "totalSteps": steps,
        "stepGoal": 8000,
        "totalDistance": steps * 0.74,
    }


def daily_stress(day: date) -> dict[str, Any]:
    return {
        "calendarDate": day.isoformat(),
        "values": {
            "overallStressLevel": 24,
            "restStressDuration": 31000,
            "lowStressDuration": 14000,
            "mediumStressDuration": 4000,
            "highStressDuration": 600,
        },
    }


def heart_rate(day: date) -> dict[str, Any]:
    return {
        "calendarDate": day.isoformat(),
        "userProfilePK": 1,
        "startTimestampGMT": f"{day.isoformat()}T00:00:00.0",
        "endTimestampGMT": f"{day.isoformat()}T23:59:59.0",
        "restingHeartRate": 52,
        "minHeartRate": 48,
        "maxHeartRate": 131,
        "lastSevenDaysAvgRestingHeartRate": 53,
        "heartRateValueDescriptors": [
            {"key": "timestamp", "index": 0},
            {"key": "heartrate", "index": 1},
        ],
        "heartRateValues": [[1_700_000_000_000, 61], [1_700_000_120_000, 64]],
    }


def sleep(day: date, *, seconds: int = 25_200) -> dict[str, Any]:
    return {
        "id": 900_000 + day.toordinal(),
        "userProfilePK": 1,
        "calendarDate": day.isoformat(),
        "sleepTimeSeconds": seconds,
        "napTimeSeconds": 0,
        "deepSleepSeconds": 5_400,
        "lightSleepSeconds": 14_400,
        "remSleepSeconds": 4_800,
        "awakeSleepSeconds": 600,
        "unmeasurableSleepSeconds": 0,
        "sleepStartTimestampGMT": 1_700_000_000_000,
        "sleepEndTimestampGMT": 1_700_000_000_000 + seconds * 1000,
        "sleepFromDevice": True,
        "retro": False,
        "sleepScores": {"overall": {"value": 78, "qualifierKey": "GOOD"}},
        "averageRespirationValue": 14.5,
        "avgHeartRate": 55.0,
    }


def hrv_summary(day: date) -> dict[str, Any]:
    return {
        "calendarDate": day.isoformat(),
        "weeklyAvg": None,
        "lastNightAvg": 43,
        "lastNight5MinHigh": 61,
        "baseline": None,
        "status": "UNBALANCED",
        "feedbackPhrase": "HRV_UNBALANCED_LOW_1",
        "createTimeStamp": f"{day.isoformat()}T06:10:00.0",
    }


def activity(activity_id: int, day: date) -> dict[str, Any]:
    return {
        "activityId": activity_id,
        "activityName": "Yürüyüş",
        "startTimeLocal": f"{day.isoformat()} 18:05:00",
        "startTimeGMT": f"{day.isoformat()} 17:05:00",
        "activityType": {"typeId": 9, "typeKey": "walking", "parentTypeId": 17},
        "distance": 3120.5,
        "duration": 2410.0,
        "averageHR": 101.0,
        "maxHR": 128.0,
        "calories": 210.0,
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
            return httpx.Response(200, text=TOKEN, headers={"content-type": "text/plain"})
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
