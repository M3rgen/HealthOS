"""The additive migration freezes the exact runtime heart invalidation contract."""

from importlib import import_module

from app.db.downgrade_guard import DISPOSABLE_TABLES
from app.models import heart_invalidation


def test_frozen_migration_matches_runtime_and_preserves_archive_protection():
    migration = import_module("migrations.versions.20261114_0036_google_heart_projection")
    assert migration.TRIGGERS == heart_invalidation.GUARDS.pg
    assert migration.FUNCTIONS == heart_invalidation.GUARDS.functions
    assert {"google_heart_hours", "google_heart_projection_state"} <= DISPOSABLE_TABLES
    assert not {"raw_health_data", "raw_health_data_versions", "heart_rate"} & DISPOSABLE_TABLES
    assert migration.down_revision == "20261112_0035"
