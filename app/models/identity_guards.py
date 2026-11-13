"""Append-only identity archive and widening-only sighting guards (migration 0012).

Importing this module registers its guards; shared functions are installed before triggers.
The SQL is frozen in the migration and compared by tests/test_identity_keys.py.
"""

from app.db.guards import GuardSet
from app.models.identity_constants import IDENTITY_TABLES, SIGHTING_TABLE

SIGHTINGS_FUNCTION = "healthos_sightings_widen_only"
FUNCTION_BODIES = {
    SIGHTINGS_FUNCTION: f"""
BEGIN
    IF NEW.data_type IS DISTINCT FROM OLD.data_type
       OR NEW.fingerprint IS DISTINCT FROM OLD.fingerprint
       OR NEW.stable_class IS DISTINCT FROM OLD.stable_class
       OR NEW.label IS DISTINCT FROM OLD.label
       OR NEW.data_source IS DISTINCT FROM OLD.data_source
       OR NEW.first_seen_at > OLD.first_seen_at
       OR NEW.last_seen_at < OLD.last_seen_at
       OR NEW.newest_record_at < OLD.newest_record_at
       OR (OLD.newest_record_at IS NOT NULL AND NEW.newest_record_at IS NULL)
       OR (OLD.complete AND NOT NEW.complete) THEN
        RAISE EXCEPTION USING MESSAGE =
            '{SIGHTING_TABLE} only widens: ' || TG_OP || ' refused';
    END IF;
    RETURN NEW;
END
""",
}

GUARDS = GuardSet(functions=FUNCTION_BODIES)
for _table in IDENTITY_TABLES:
    GUARDS.append_only(_table)
GUARDS.postgresql(
    f"{SIGHTING_TABLE}_widen_only",
    SIGHTING_TABLE,
    f"BEFORE UPDATE ON {SIGHTING_TABLE} FOR EACH ROW",
    SIGHTINGS_FUNCTION,
)
GUARDS.postgresql(
    f"{SIGHTING_TABLE}_no_delete",
    SIGHTING_TABLE,
    f"BEFORE DELETE ON {SIGHTING_TABLE} FOR EACH ROW",
    "healthos_refuse_change",
)
GUARDS.postgresql(
    f"{SIGHTING_TABLE}_no_truncate",
    SIGHTING_TABLE,
    f"BEFORE TRUNCATE ON {SIGHTING_TABLE} FOR EACH STATEMENT",
    "healthos_refuse_change",
)


POSTGRESQL_DDL = GUARDS.postgresql_ddl
