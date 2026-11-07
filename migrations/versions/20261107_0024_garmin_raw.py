"""Raw archive of the Garmin Connect Personal API (app/models/garmin.py).

Three new tables: ``garmin_api_responses`` (every HTTP exchange, body byte for byte) and
``garmin_raw_records`` (one row per kind, key and distinct payload; the earlier payload of a
day stays when a later fetch returns another), both append-only, and ``garmin_pull_state``
(per kind: last attempt, success, error and the backfill checkpoint; bookkeeping).

Only new tables, their indexes and guards: no ALTER of an existing table and no foreign key
onto one (the one key is between the two new tables), so no lock is taken on anything that
exists and a running pg_dump never blocks or fails it. No existing row is read or changed.
Frozen SQL, independent of application modules.

downgrade() refuses while either append-only table holds a row (Garmin's responses and
records would be lost); the state table is dropped with them.
"""

import sqlalchemy as sa
from alembic import op

revision = "20261107_0024"
down_revision = "20261102_0023"
branch_labels = None
depends_on = None

RESPONSES = "garmin_api_responses"
RECORDS = "garmin_raw_records"
STATE = "garmin_pull_state"
GUARDED = (RESPONSES, RECORDS)
POSTGRESQL_DDL = []
SQLITE_DDL = []
for _table in GUARDED:
    POSTGRESQL_DDL += [
        f"CREATE TRIGGER {_table}_append_only\nBEFORE UPDATE OR DELETE ON {_table} "
        "FOR EACH ROW\nEXECUTE FUNCTION healthos_refuse_change()",
        f"CREATE TRIGGER {_table}_no_truncate\nBEFORE TRUNCATE ON {_table} "
        "FOR EACH STATEMENT\nEXECUTE FUNCTION healthos_refuse_change()",
    ]
    SQLITE_DDL += [
        f"CREATE TRIGGER {_table}_no_{operation.lower()}\nBEFORE {operation} ON {_table}\n"
        f"BEGIN\n    SELECT RAISE(ABORT, '{_table} keeps every record: {operation} refused');"
        "\nEND"
        for operation in ("UPDATE", "DELETE")
    ]
POSTGRESQL_DDL += [
    f"ALTER TABLE {_table} ENABLE ALWAYS TRIGGER {_table}_{suffix}"
    for _table in GUARDED
    for suffix in ("append_only", "no_truncate")
]


def _id() -> sa.types.TypeEngine:
    return sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def _json() -> sa.types.TypeEngine:
    return sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        RESPONSES,
        sa.Column("id", _id(), autoincrement=True, nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("context_id", sa.String(160), nullable=True),
        sa.Column("method", sa.String(8), nullable=False),
        sa.Column("path", sa.String(512), nullable=False),
        sa.Column("query", sa.Text(), nullable=True),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("headers", sa.JSON(), nullable=False),
        sa.Column("body", sa.LargeBinary(), nullable=False),
        sa.Column("body_sha256", sa.String(64), nullable=False),
        sa.Column("body_size", sa.Integer(), nullable=False),
        sa.Column("complete", sa.Boolean(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{RESPONSES}")),
    )
    op.create_index(f"ix_{RESPONSES}_kind_fetched", RESPONSES, ["kind", "fetched_at"])
    op.create_table(
        RECORDS,
        sa.Column("id", _id(), autoincrement=True, nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("body", _json(), nullable=False),
        sa.Column("body_sha256", sa.String(64), nullable=False),
        sa.Column("response_id", _id(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["response_id"], [f"{RESPONSES}.id"], name=op.f(f"fk_{RECORDS}_response_id_{RESPONSES}")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{RECORDS}")),
        sa.UniqueConstraint("kind", "key", "body_sha256", name=f"uq_{RECORDS}_version"),
    )
    op.create_index(f"ix_{RECORDS}_kind_key", RECORDS, ["kind", "key"])
    op.create_table(
        STATE,
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("backfill_since", sa.Date(), nullable=True),
        sa.Column("backfill_until", sa.Date(), nullable=True),
        sa.Column("backfill_next", sa.Date(), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("kind", name=op.f(f"pk_{STATE}")),
    )
    dialect = op.get_context().dialect.name
    for statement in POSTGRESQL_DDL if dialect == "postgresql" else SQLITE_DDL:
        op.execute(statement)


def downgrade() -> None:
    if op.get_context().as_sql:
        raise RuntimeError("Garmin raw tables must be checked before an online downgrade")
    for table in GUARDED:
        if op.get_bind().scalar(sa.text(f"SELECT count(*) FROM {table}")):
            raise RuntimeError(f"{table} holds evidence; downgrade refused")
    op.drop_table(STATE)
    op.drop_table(RECORDS)
    op.drop_table(RESPONSES)
