"""Add immutable original activity files and completed download evidence.

A result is a downloaded file, a permanent 404/410 or an ``invalid`` refusal (reason code and
the received body's digest/size only) for one raw list version.

No existing row is read or changed. Downgrade locks both new tables before checking
for evidence and refuses to discard any row, including permanent absence evidence.
"""

import sqlalchemy as sa
from alembic import op

revision = "20261115_0038"
down_revision = "20261115_0037"
branch_labels = None
depends_on = None

TABLES = ("garmin_activity_files", "garmin_activity_file_results")
POSTGRESQL_DDL = tuple(
    statement
    for table in TABLES
    for statement in (
        f"CREATE TRIGGER {table}_append_only\nBEFORE UPDATE OR DELETE ON {table} "
        "FOR EACH ROW\nEXECUTE FUNCTION healthos_refuse_change()",
        f"CREATE TRIGGER {table}_no_truncate\nBEFORE TRUNCATE ON {table} "
        "FOR EACH STATEMENT\nEXECUTE FUNCTION healthos_refuse_change()",
        f"ALTER TABLE {table} ENABLE ALWAYS TRIGGER {table}_append_only",
        f"ALTER TABLE {table} ENABLE ALWAYS TRIGGER {table}_no_truncate",
    )
)
SQLITE_DDL = tuple(
    f"CREATE TRIGGER {table}_no_{operation.lower()}\nBEFORE {operation} ON {table}\nBEGIN\n"
    f"    SELECT RAISE(ABORT, '{table} keeps every record: {operation} refused');\nEND"
    for table in TABLES
    for operation in ("UPDATE", "DELETE")
)


def _identity_columns():
    return (
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True),
        sa.Column("activity_id", sa.Text(), nullable=False),
        sa.Column("activity_sha256", sa.String(64), nullable=False),
        sa.Column(
            "raw_record_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            sa.ForeignKey("garmin_raw_records.id"),
            nullable=False,
        ),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
    )


def upgrade() -> None:
    op.create_table(
        TABLES[0],
        *_identity_columns(),
        sa.Column("body", sa.LargeBinary(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.UniqueConstraint("activity_sha256", "sha256", name="uq_garmin_activity_files_version"),
        sa.CheckConstraint("size >= 14 AND size <= 8388608", name="bounded_size"),
    )
    op.create_table(
        TABLES[1],
        *_identity_columns(),
        sa.Column(
            "file_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            sa.ForeignKey("garmin_activity_files.id"),
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(32)),
        sa.Column("content_sha256", sa.String(64)),
        sa.Column("content_size", sa.BigInteger()),
        sa.UniqueConstraint("raw_record_id", name="uq_garmin_activity_file_results_raw"),
        sa.CheckConstraint(
            "(status = 'downloaded' AND file_id IS NOT NULL AND reason IS NULL) OR "
            "(status IN ('not_found', 'gone') AND file_id IS NULL AND reason IS NULL) OR "
            "(status = 'invalid' AND file_id IS NULL AND reason IS NOT NULL)",
            name="outcome",
        ),
    )
    op.create_index(
        "ix_garmin_activity_file_results_activity", TABLES[1], ["activity_sha256", "status"]
    )
    dialect = op.get_context().dialect.name
    for statement in POSTGRESQL_DDL if dialect == "postgresql" else SQLITE_DDL:
        op.execute(statement)


def downgrade() -> None:
    if op.get_context().as_sql:
        raise RuntimeError("Garmin activity files must be checked before an online downgrade")
    if op.get_context().dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")
        op.execute(
            "LOCK TABLE garmin_activity_files, garmin_activity_file_results "
            "IN ACCESS EXCLUSIVE MODE"
        )
    for table in TABLES:
        if op.get_bind().scalar(sa.text(f"SELECT 1 FROM {table} LIMIT 1")) is not None:
            raise RuntimeError(f"{table} holds evidence; downgrade refused")
    for table in reversed(TABLES):
        op.drop_table(table)
