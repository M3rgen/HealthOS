"""Transactional dirty revisions, including SQL bulk writers and identity undo.

The trigger DDL is frozen in migrations 0018/0020/0028. Only disposable derived rows are
updated. A shared PostgreSQL advisory transaction lock fences mutations against a builder's
exclusive lock: even a previously unseen day cannot miss a concurrently committed change.
Unrelated raw and identity data types bypass that fence before entering the function.

On PostgreSQL a row trigger only records which days it dirties (``derived_day_pending``, keyed
by the writing transaction, no row lock on ``derived_days``); a deferred constraint trigger
bumps them all at commit, locking in primary-key order. Every writer takes its day locks once,
last, in the same order, so two writers cannot deadlock on ``derived_days``, and a page bumps
each day once instead of once per row.
"""

from collections.abc import Iterable

from app.db.guards import GuardSet
from app.db.postgres import BUILD_LOCK

DAILY = "'daily-resting-heart-rate','daily-heart-rate-variability','daily-oxygen-saturation'"
DERIVED_TYPES = f"'steps','sleep',{DAILY}"
TYPE_FILTERED_TABLES = {"raw_health_data", "raw_health_data_supersessions", "raw_source_devices"}


def _scope(ref: str):
    return f"""(
      ({ref}.data_type = 'steps' AND {ref}.source_start_time < window_stop
        AND {ref}.source_end_time > window_start)
      OR ({ref}.data_type = 'sleep' AND {ref}.source_end_time >= window_start
        AND {ref}.source_end_time < window_stop)
      OR ({ref}.data_type IN ({DAILY}) AND {ref}.source_start_time >= civil_start
        AND {ref}.source_start_time < civil_stop)
      OR ({ref}.data_type = 'steps' AND EXISTS (SELECT 1 FROM steps n
        WHERE n.google_data_point_id = {ref}.google_data_point_id
          AND n.start_time < window_stop AND n.end_time > window_start))
      OR ({ref}.data_type = 'sleep' AND EXISTS (SELECT 1 FROM sleep_sessions n
        WHERE n.google_data_point_id = {ref}.google_data_point_id
          AND n.end_time >= window_start AND n.end_time < window_stop)))"""


def _held(ref: str):
    return f"""EXISTS (SELECT 1 FROM raw_health_data r
      JOIN raw_health_data_supersessions s ON s.old_raw_id = r.id
      WHERE s.new_raw_id = {ref}.id AND {_scope("r")})"""


# Which days a set of source rows dirties. ``_scope`` above is the same test per row;
# here each row becomes one or more spans first, so the per-day comparison is a
# handful of constants instead of a correlated lookup per derived day.
_SPAN_MATCH = f"""(
      (kind = 'steps' AND s < window_stop AND e > window_start)
      OR (kind = 'sleep' AND e >= window_start AND e < window_stop)
      OR (kind IN ({DAILY}) AND s >= civil_start AND s < civil_stop))"""


def _pg_spans(sources: Iterable[str]):
    """``WITH``: the rows of ``sources`` (data_type, s, e, gid) and their normalized rows."""
    union = "\n        UNION ALL ".join(sources)
    return f"""WITH source (data_type, s, e, gid) AS MATERIALIZED (
        {union}),
      spans AS MATERIALIZED (
        SELECT data_type AS kind, s, e FROM source
        UNION ALL SELECT 'steps', n.start_time, n.end_time FROM source r
          JOIN steps n ON n.google_data_point_id = r.gid WHERE r.data_type = 'steps'
        UNION ALL SELECT 'sleep', n.start_time, n.end_time FROM source r
          JOIN sleep_sessions n ON n.google_data_point_id = r.gid WHERE r.data_type = 'sleep')
      """


def _raw_row(ref: str):
    return (
        f"SELECT {ref}.data_type, {ref}.source_start_time, "
        f"{ref}.source_end_time, {ref}.google_data_point_id"
    )


def _raw_select(where: str):
    return (
        "SELECT r.data_type, r.source_start_time, r.source_end_time, r.google_data_point_id "
        f"FROM raw_health_data r {where}"
    )


def _held_source(ref: str):
    return _raw_select(
        f"JOIN raw_health_data_supersessions s ON s.old_raw_id = r.id WHERE s.new_raw_id = {ref}.id"
    )


def _source_rows(table: str, ref: str):
    if table == "raw_health_data":
        return [_raw_row(ref), _held_source(ref)]
    if table == "raw_health_data_supersessions":
        return [_raw_select(f"WHERE r.id IN ({ref}.old_raw_id, {ref}.new_raw_id)")]
    if table == "raw_source_devices":
        return [
            _raw_select(
                "JOIN raw_health_data_supersessions s ON s.old_raw_id = r.id "
                f"WHERE s.data_type = {ref}.data_type AND "
                f"(s.old_fingerprint = {ref}.fingerprint OR s.new_fingerprint = {ref}.fingerprint)"
            )
        ]
    kinds = DAILY_TABLE_TYPES.get(table)
    if kinds is not None:
        return [
            _raw_select(
                f"WHERE r.data_type IN ({kinds}) "
                f"AND r.google_data_point_id = {ref}.google_data_point_id"
            )
        ]
    return None


# table -> (operations, value-bearing columns, affected day predicate per OLD/NEW)
SPECS = {
    "raw_health_data": (
        ("INSERT", "UPDATE"),
        (
            "raw_json",
            "source_start_time",
            "source_end_time",
            "source_timezone",
            "source_deleted_at",
            "is_quarantined",
        ),
        lambda ref: f"({_scope(ref)} OR {_held(ref)})",
    ),
    "steps": (
        ("INSERT", "UPDATE", "DELETE"),
        ("start_time", "end_time", "count", "is_quarantined", "google_data_point_id"),
        lambda ref: f"{ref}.start_time < window_stop AND {ref}.end_time > window_start",
    ),
    "sleep_sessions": (
        ("INSERT", "UPDATE", "DELETE"),
        (
            "start_time",
            "end_time",
            "duration_seconds",
            "awake_seconds",
            "is_quarantined",
            "google_data_point_id",
        ),
        lambda ref: f"{ref}.end_time >= window_start AND {ref}.end_time < window_stop",
    ),
    "raw_health_data_supersessions": (
        ("INSERT",),
        (),
        lambda ref: (
            f"EXISTS (SELECT 1 FROM raw_health_data r WHERE "
            f"r.id IN ({ref}.old_raw_id, {ref}.new_raw_id) AND {_scope('r')})"
        ),
    ),
    "raw_source_devices": (
        ("INSERT",),
        (),
        lambda ref: (
            f"""EXISTS (SELECT 1 FROM raw_health_data r
          JOIN raw_health_data_supersessions s ON s.old_raw_id = r.id
          WHERE s.data_type = {ref}.data_type AND
            (s.old_fingerprint = {ref}.fingerprint OR s.new_fingerprint = {ref}.fingerprint)
            AND {_scope("r")})"""
        ),
    ),
    "manual_measurements": (
        ("INSERT",),
        (),
        lambda ref: f"{ref}.measured_at < window_stop",
    ),
    "manual_measurement_flag_events": (
        ("INSERT",),
        (),
        lambda ref: (
            f"EXISTS (SELECT 1 FROM manual_measurements m "
            f"WHERE m.id = {ref}.measurement_id AND m.measured_at < window_stop)"
        ),
    ),
}
DAILY_TABLE_TYPES = {
    "heart_rate_variability": "'daily-heart-rate-variability'",
    "oxygen_saturation": "'daily-oxygen-saturation'",
    "resting_heart_rate": "'daily-resting-heart-rate'",
}
for _table, _types in DAILY_TABLE_TYPES.items():
    SPECS[_table] = (
        ("INSERT", "UPDATE", "DELETE"),
        ("is_quarantined", "google_data_point_id"),
        lambda ref, kinds=_types: (
            f"EXISTS (SELECT 1 FROM raw_health_data r WHERE "
            f"r.data_type IN ({kinds}) AND r.google_data_point_id = {ref}.google_data_point_id "
            f"AND {_scope('r')})"
        ),
    )


# The normalized tables a Garmin row can reach: its key never joins a raw row, so a Garmin
# row cannot change a derived day (app/core/providers.py) and takes neither fence nor bump.
PROVIDER_TABLES = {"steps", "sleep_sessions", *DAILY_TABLE_TYPES}
GARMIN = "'GARMIN'"
PENDING_TABLE = "derived_day_pending"
DRAIN_TRIGGER = f"{PENDING_TABLE}_drain"
DRAIN_FUNCTION = f"healthos_{DRAIN_TRIGGER}"
DRAIN_BODY = f"""
BEGIN
    PERFORM 1 FROM derived_days d
      JOIN {PENDING_TABLE} p ON p.xact = pg_current_xact_id()::text::bigint
        AND p.day = d.day AND p.timezone = d.timezone AND p.method = d.method
      ORDER BY d.day, d.timezone, d.method FOR UPDATE OF d;
    UPDATE derived_days d SET revision = d.revision + 1 FROM {PENDING_TABLE} p
      WHERE p.xact = pg_current_xact_id()::text::bigint
        AND p.day = d.day AND p.timezone = d.timezone AND p.method = d.method;
    DELETE FROM {PENDING_TABLE} WHERE xact = pg_current_xact_id()::text::bigint;
    RETURN NULL;
END
"""


def _pending_body(table: str, refs: Iterable[str], affected: str):
    arms = [arm for ref in refs for arm in _source_rows(table, ref) or ()]
    if arms:
        cte, where = _pg_spans(arms), f"EXISTS (SELECT 1 FROM spans WHERE {_SPAN_MATCH})"
    else:
        cte, where = "", f"({affected})"
    return (
        f"INSERT INTO {PENDING_TABLE} (xact, day, timezone, method)\n"
        f"      {cte}SELECT pg_current_xact_id()::text::bigint, d.day, d.timezone, d.method\n"
        f"      FROM derived_days d WHERE {where}\n      ON CONFLICT DO NOTHING;"
    )


def statements():
    pg_triggers, functions = {}, {}
    for table, (operations, columns, affected) in SPECS.items():
        for operation in operations:
            name = f"derived_{table}_{operation.lower()}"
            refs = (
                ("OLD", "NEW")
                if operation == "UPDATE"
                else (("OLD",) if operation == "DELETE" else ("NEW",))
            )
            predicate = " OR ".join(f"({affected(ref)})" for ref in refs)
            changed = " OR ".join(
                f"NEW.{column} IS DISTINCT FROM OLD.{column}" for column in columns
            )
            condition = f" WHEN ({changed})" if operation == "UPDATE" else ""
            pg_condition = condition
            if table in TYPE_FILTERED_TABLES:
                # Only UPDATE has both rows. Check either side without narrowing the
                # existing value-change test; INSERT must never reference OLD.
                kinds = " OR ".join(f"{ref}.data_type IN ({DERIVED_TYPES})" for ref in refs)
                pg_condition = f" WHEN (({kinds})" + (
                    f" AND ({changed}))" if operation == "UPDATE" else ")"
                )
            if table in PROVIDER_TABLES:
                provider = " OR ".join(f"{ref}.provider <> {GARMIN}" for ref in refs)
                pg_condition = f" WHEN (({provider})" + (
                    f" AND ({changed}))" if operation == "UPDATE" else ")"
                )
            function = f"healthos_{name}"
            functions[function] = (
                f"\nBEGIN\n    PERFORM pg_advisory_xact_lock_shared({BUILD_LOCK});\n    "
                f"{_pending_body(table, refs, predicate)}\n    RETURN NULL;\nEND\n"
            )
            pg_triggers[name] = (
                table,
                f"CREATE TRIGGER {name} AFTER {operation} ON {table} FOR EACH ROW{pg_condition} "
                f"EXECUTE FUNCTION {function}()",
            )
    return pg_triggers, functions


GUARDS = GuardSet(function_separator=" ")
GUARDS.pg, GUARDS.functions = statements()
GUARDS.pg[DRAIN_TRIGGER] = (
    PENDING_TABLE,
    f"CREATE CONSTRAINT TRIGGER {DRAIN_TRIGGER} AFTER INSERT ON {PENDING_TABLE} "
    f"DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION {DRAIN_FUNCTION}()",
)
GUARDS.functions[DRAIN_FUNCTION] = DRAIN_BODY
GUARDS.triggers = tuple((table, name, f"healthos_{name}") for name, (table, _) in GUARDS.pg.items())
POSTGRESQL_DDL = GUARDS.postgresql_ddl
