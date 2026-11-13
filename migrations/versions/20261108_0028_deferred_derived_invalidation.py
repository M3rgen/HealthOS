"""Deferred derived-day invalidation: per-row bumps become one ordered bump at commit.

Row triggers used to UPDATE ``derived_days`` once per source row, locking the day rows in
page order. Two writers of overlapping days deadlocked. Now a row trigger records the days it
dirties in ``derived_day_pending`` (keyed by the transaction) and a deferred constraint trigger
bumps them at commit, in primary-key order. The trigger names, WHEN conditions and advisory
fence are kept, except that normalized rows of the Garmin provider (which never join a raw
row and cannot change a derived day) no longer take the fence or dirty a day. Function bodies
are frozen here, independent of application modules; the matching copy lives in
app/models/derived_invalidation.py. SQLite keeps its 0018 triggers.

Downgrade restores the previous trigger bodies. It refuses to run if a pending row exists
(none can be: rows live only inside a writing transaction).
"""

import sqlalchemy as sa
from alembic import op

revision = "20261108_0028"
down_revision = "20261108_0027"
branch_labels = None
depends_on = None

BUILD_LOCK = 72184518
DAILY = "'daily-resting-heart-rate','daily-heart-rate-variability','daily-oxygen-saturation'"
DERIVED_TYPES = f"'steps','sleep',{DAILY}"
TYPE_FILTERED_TABLES = {"raw_health_data", "raw_health_data_supersessions", "raw_source_devices"}
DAILY_TABLE_TYPES = {
    "heart_rate_variability": "'daily-heart-rate-variability'",
    "oxygen_saturation": "'daily-oxygen-saturation'",
    "resting_heart_rate": "'daily-resting-heart-rate'",
}
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
_SPAN_MATCH = f"""(
      (kind = 'steps' AND s < window_stop AND e > window_start)
      OR (kind = 'sleep' AND e >= window_start AND e < window_stop)
      OR (kind IN ({DAILY}) AND s >= civil_start AND s < civil_stop))"""


def _scope(ref):
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


def _held(ref):
    return f"""EXISTS (SELECT 1 FROM raw_health_data r
      JOIN raw_health_data_supersessions s ON s.old_raw_id = r.id
      WHERE s.new_raw_id = {ref}.id AND {_scope("r")})"""


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


def _pg_spans(sources):
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


def _raw_row(ref):
    return (
        f"SELECT {ref}.data_type, {ref}.source_start_time, "
        f"{ref}.source_end_time, {ref}.google_data_point_id"
    )


def _raw_select(where):
    return (
        "SELECT r.data_type, r.source_start_time, r.source_end_time, r.google_data_point_id "
        f"FROM raw_health_data r {where}"
    )


def _held_source(ref):
    return _raw_select(
        f"JOIN raw_health_data_supersessions s ON s.old_raw_id = r.id WHERE s.new_raw_id = {ref}.id"
    )


def _source_rows(table, ref):
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


def _pending_body(table, refs, affected):
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


def statements(*, previous):
    """(function -> body, trigger -> (table, CREATE OR REPLACE TRIGGER ...), ...)."""
    functions, triggers = {}, {}
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
            if table in TYPE_FILTERED_TABLES:
                kinds = " OR ".join(f"{ref}.data_type IN ({DERIVED_TYPES})" for ref in refs)
                condition = f" WHEN (({kinds})" + (
                    f" AND ({changed}))" if operation == "UPDATE" else ")"
                )
            elif table in PROVIDER_TABLES and not previous:
                provider = " OR ".join(f"{ref}.provider <> {GARMIN}" for ref in refs)
                condition = f" WHEN (({provider})" + (
                    f" AND ({changed}))" if operation == "UPDATE" else ")"
                )
            function = f"healthos_{name}"
            if previous:
                body = f"UPDATE derived_days SET revision = revision + 1 WHERE {predicate};"
            else:
                body = _pending_body(table, refs, predicate)
            functions[function] = (
                f"\nBEGIN\n    PERFORM pg_advisory_xact_lock_shared({BUILD_LOCK});\n    {body}\n"
                "    RETURN NULL;\nEND\n"
            )
            triggers[name] = (
                table,
                f"CREATE TRIGGER {name} AFTER {operation} ON {table} FOR EACH ROW{condition} "
                f"EXECUTE FUNCTION {function}()",
            )
    return functions, triggers


FUNCTION_BODIES, TRIGGER_SQL = statements(previous=False)
PREVIOUS_FUNCTION_BODIES, PREVIOUS_TRIGGER_SQL = statements(previous=True)
FUNCTION_BODIES[DRAIN_FUNCTION] = DRAIN_BODY
DRAIN_SQL = (
    f"CREATE CONSTRAINT TRIGGER {DRAIN_TRIGGER} AFTER INSERT ON {PENDING_TABLE} "
    f"DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION {DRAIN_FUNCTION}()"
)


def _replace(trigger_sql):
    return trigger_sql.replace("CREATE TRIGGER", "CREATE OR REPLACE TRIGGER")


def _apply(functions, triggers):
    for name, body in functions.items():
        op.execute(
            f"CREATE OR REPLACE FUNCTION {name}() RETURNS trigger LANGUAGE plpgsql AS $${body}$$"
        )
    for name, (table, sql) in triggers.items():
        op.execute(_replace(sql))
        op.execute(f"ALTER TABLE {table} ENABLE ALWAYS TRIGGER {name}")


def upgrade() -> None:
    op.create_table(
        PENDING_TABLE,
        sa.Column("xact", sa.BigInteger(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("timezone", sa.String(128), nullable=False),
        sa.Column("method", sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint(
            "xact", "day", "timezone", "method", name=op.f("pk_derived_day_pending")
        ),
    )
    if op.get_context().dialect.name != "postgresql":
        return
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        f"CREATE FUNCTION {DRAIN_FUNCTION}() RETURNS trigger LANGUAGE plpgsql AS $${DRAIN_BODY}$$"
    )
    op.execute(DRAIN_SQL)
    op.execute(f"ALTER TABLE {PENDING_TABLE} ENABLE ALWAYS TRIGGER {DRAIN_TRIGGER}")
    _apply({k: v for k, v in FUNCTION_BODIES.items() if k != DRAIN_FUNCTION}, TRIGGER_SQL)


def downgrade() -> None:
    if op.get_context().as_sql:
        raise RuntimeError(f"{PENDING_TABLE} must be checked before an online downgrade")
    bind = op.get_bind()
    pending = bind.scalar(sa.text(f"SELECT count(*) FROM {PENDING_TABLE}"))
    if pending:
        raise RuntimeError(
            f"{PENDING_TABLE} holds {pending} row(s), which only exists inside a writing "
            "transaction; a committed row means a day's invalidation did not run. Rebuild derived "
            "days before downgrading."
        )
    if op.get_context().dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")
        _apply(PREVIOUS_FUNCTION_BODIES, PREVIOUS_TRIGGER_SQL)
        op.execute(f"DROP TRIGGER {DRAIN_TRIGGER} ON {PENDING_TABLE}")
        op.execute(f"DROP FUNCTION {DRAIN_FUNCTION}()")
    op.drop_table(PENDING_TABLE)
