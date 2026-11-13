"""Explicitly unlocked Garmin construction shared by offline ingestion tests."""

from app.db.contracts import unlocked_for_tests
from app.services.providers.garmin_pull import GarminPull


def unlocked(settings, sessions, **kwargs) -> GarminPull:
    return GarminPull(settings, sessions, **kwargs).owned_by(unlocked_for_tests(sessions))
