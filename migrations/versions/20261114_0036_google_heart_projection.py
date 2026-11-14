"""Add disposable original-sample packets; the raw archive is unchanged.

Row triggers with short WHEN clauses fire only for heart rows; each transaction bumps an hour
once (its first touch, deduplicated in google_heart_pending, emptied by a deferred trigger
at commit).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "20261114_0036"
down_revision = "20261112_0035"
branch_labels = None
depends_on = None


HEART_LOCK = 72184536
PENDING_TABLE = "google_heart_pending"
DRAIN_TRIGGER = f"{PENDING_TABLE}_drain"
DRAIN_FUNCTION = f"healthos_{DRAIN_TRIGGER}"
TABLES = {
    "raw_health_data": (
        "data_type",
        "raw_json",
        "source_start_time",
        "source_deleted_at",
        "is_quarantined",
    ),
    "heart_rate": ("google_data_point_id", "is_quarantined", "provider"),
    "raw_health_data_supersessions": (),
    "raw_source_devices": (),
}


def statements() -> tuple[dict, dict]:
    triggers, functions = {}, {}
    for table, columns in TABLES.items():
        for operation in ("INSERT", "UPDATE", "DELETE"):
            refs = (
                ("OLD", "NEW")
                if operation == "UPDATE"
                else (("OLD",) if operation == "DELETE" else ("NEW",))
            )
            name = f"heart_{table}_{operation.lower()}"
            # Keep WHEN short: PostgreSQL compiles it for every statement, and the archive is
            # written one row per statement, so a long WHEN costs every data type. The
            # value-change test of an UPDATE runs inside the (heart-only) functions instead.
            condition = (
                " OR ".join(f"{ref}.data_type = 'heart-rate'" for ref in refs)
                if table != "heart_rate"
                else " OR ".join(f"{ref}.provider <> 'GARMIN'" for ref in refs)
            )
            changed = ""
            fence = "healthos_heart_fence"
            if operation == "UPDATE" and columns:
                changed = " OR ".join(f"OLD.{c} IS DISTINCT FROM NEW.{c}" for c in columns)
                fence = f"healthos_{name}_fence"
                functions[fence] = f"""
BEGIN
    IF {changed} THEN PERFORM pg_advisory_xact_lock({HEART_LOCK}); END IF;
    RETURN NEW;
END
"""
            triggers[name + "_fence"] = (
                table,
                f"CREATE TRIGGER {name}_fence BEFORE {operation} ON {table} "
                f"FOR EACH ROW WHEN ({condition}) EXECUTE FUNCTION {fence}()",
            )
            union = " UNION ".join(_sources(table, ref) for ref in refs)
            functions["healthos_" + name] = _body(union, changed)
            triggers[name] = (
                table,
                f"CREATE TRIGGER {name} AFTER {operation} ON {table} "
                f"FOR EACH ROW WHEN ({condition}) EXECUTE FUNCTION healthos_{name}()",
            )
    functions["healthos_heart_fence"] = f"""
BEGIN
    PERFORM pg_advisory_xact_lock({HEART_LOCK});
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END
"""
    _truncate(triggers, functions)
    _drain(triggers, functions)
    return triggers, functions


def _body(union: str, changed: str = "") -> str:
    # The pending row dedupes the hour per transaction: only its first touch bumps.
    unchanged = f"\n    IF NOT ({changed}) THEN RETURN NULL; END IF;" if changed else ""
    return f"""
BEGIN{unchanged}
    WITH fresh AS (
      INSERT INTO {PENDING_TABLE} (xact, bucket)
        SELECT DISTINCT pg_current_xact_id()::text::bigint, date_trunc('hour', t, 'UTC')
        FROM ({union}) affected WHERE t IS NOT NULL
        ON CONFLICT DO NOTHING RETURNING bucket)
    INSERT INTO google_heart_hours (bucket, revision, built_revision)
      SELECT bucket, 1, 0 FROM fresh ORDER BY 1
      ON CONFLICT (bucket) DO UPDATE SET revision = google_heart_hours.revision + 1;
    RETURN NULL;
END
"""


def _truncate(triggers: dict, functions: dict) -> None:
    # TRUNCATE already holds the table lock fencing the builder's source SELECT.
    # Taking the advisory fence after that lock would reverse the builder's lock order.
    functions["healthos_heart_truncate"] = """
BEGIN
    UPDATE google_heart_hours SET revision = revision + 1;
    RETURN NULL;
END
"""
    triggers["heart_rate_projection_truncate"] = (
        "heart_rate",
        "CREATE TRIGGER heart_rate_projection_truncate AFTER TRUNCATE ON heart_rate "
        "FOR EACH STATEMENT EXECUTE FUNCTION healthos_heart_truncate()",
    )


def _drain(triggers: dict, functions: dict) -> None:
    # Pending rows only dedupe one transaction's bumps; none may outlive its commit.
    functions[DRAIN_FUNCTION] = f"""
BEGIN
    DELETE FROM {PENDING_TABLE} WHERE xact = NEW.xact;
    RETURN NULL;
END
"""
    triggers[DRAIN_TRIGGER] = (
        PENDING_TABLE,
        f"CREATE CONSTRAINT TRIGGER {DRAIN_TRIGGER} AFTER INSERT ON {PENDING_TABLE} "
        f"DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION {DRAIN_FUNCTION}()",
    )


def _sources(table: str, ref: str) -> str:
    if table == "raw_health_data":
        return (
            f"SELECT {ref}.source_start_time AS t WHERE {ref}.data_type = 'heart-rate'"
            " UNION SELECT r.source_start_time AS t FROM raw_health_data_supersessions s"
            " JOIN raw_health_data r ON r.id = s.old_raw_id"
            f" WHERE s.new_raw_id = {ref}.id AND r.data_type = 'heart-rate'"
        )
    if table == "heart_rate":
        return (
            "SELECT r.source_start_time AS t FROM raw_health_data r"
            " WHERE r.data_type = 'heart-rate'"
            f" AND r.google_data_point_id = {ref}.google_data_point_id"
        )
    if table == "raw_health_data_supersessions":
        return (
            "SELECT r.source_start_time AS t FROM raw_health_data r"
            f" WHERE r.id IN ({ref}.old_raw_id, {ref}.new_raw_id)"
            f" AND {ref}.data_type = 'heart-rate'"
        )
    return (
        "SELECT r.source_start_time AS t FROM raw_health_data_supersessions s"
        " JOIN raw_health_data r ON r.id IN (s.old_raw_id, s.new_raw_id)"
        f" WHERE s.data_type = {ref}.data_type AND {ref}.data_type = 'heart-rate'"
        f" AND (s.old_fingerprint = {ref}.fingerprint OR s.new_fingerprint = {ref}.fingerprint)"
    )


TRIGGERS, FUNCTIONS = statements()


def upgrade() -> None:
    if op.get_context().dialect.name == "postgresql":
        # Trigger DDL on the archive tables queues every writer behind it: never wait on an
        # open sync transaction indefinitely (as 0018 and 0028), fail with nothing changed.
        op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "google_heart_hours",
        sa.Column("bucket", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default=sa.text("1")),
        sa.Column("built_revision", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("method", sa.String(64)),
        sa.Column("samples", sa.JSON().with_variant(JSONB(), "postgresql")),
        sa.Column("digest", sa.String(64)),
    )
    op.create_table(
        "google_heart_projection_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("next_hour", sa.DateTime(timezone=True)),
        sa.Column("stop", sa.DateTime(timezone=True)),
        sa.Column("ready", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.create_table(
        "google_heart_pending",
        sa.Column("xact", sa.BigInteger(), primary_key=True),
        sa.Column("bucket", sa.DateTime(timezone=True), primary_key=True),
    )

    if op.get_context().dialect.name == "postgresql":
        for name, body in FUNCTIONS.items():
            op.execute(f"CREATE FUNCTION {name}() RETURNS trigger LANGUAGE plpgsql AS $${body}$$")
        for _table, sql in TRIGGERS.values():
            op.execute(sql)
        for name, (table, _) in TRIGGERS.items():
            op.execute(f"ALTER TABLE {table} ENABLE ALWAYS TRIGGER {name}")


def downgrade() -> None:
    if op.get_context().dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")
        for name, (table, _) in TRIGGERS.items():
            op.execute(f"DROP TRIGGER {name} ON {table}")
        for name in FUNCTIONS:
            op.execute(f"DROP FUNCTION {name}()")
    # These tables contain only reproducible read models, never source/history rows.
    op.drop_table("google_heart_pending")
    op.drop_table("google_heart_projection_state")
    op.drop_table("google_heart_hours")
