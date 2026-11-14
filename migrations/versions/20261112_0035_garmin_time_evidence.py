"""Add immutable heart offset metadata and completed daily time choices."""

import sqlalchemy as sa
from alembic import op

revision = "20261112_0035"
down_revision = "20261112_0034"
branch_labels = None
depends_on = None

TABLES = ("garmin_time_evidence", "garmin_day_times")
POSTGRESQL_DDL = tuple(
    statement
    for TABLE in TABLES
    for statement in (
        f"CREATE TRIGGER {TABLE}_append_only\nBEFORE UPDATE OR DELETE ON {TABLE} "
        "FOR EACH ROW\nEXECUTE FUNCTION healthos_refuse_change()",
        f"CREATE TRIGGER {TABLE}_no_truncate\nBEFORE TRUNCATE ON {TABLE} "
        "FOR EACH STATEMENT\nEXECUTE FUNCTION healthos_refuse_change()",
        f"ALTER TABLE {TABLE} ENABLE ALWAYS TRIGGER {TABLE}_append_only",
        f"ALTER TABLE {TABLE} ENABLE ALWAYS TRIGGER {TABLE}_no_truncate",
    )
)
SQLITE_DDL = tuple(
    f"CREATE TRIGGER {TABLE}_no_{operation.lower()}\nBEFORE {operation} ON {TABLE}\nBEGIN\n"
    f"    SELECT RAISE(ABORT, '{TABLE} keeps every record: {operation} refused');\nEND"
    for TABLE in TABLES
    for operation in ("UPDATE", "DELETE")
)


def upgrade() -> None:
    op.create_table(
        TABLES[0],
        sa.Column(
            "raw_record_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            sa.ForeignKey("garmin_raw_records.id"),
            primary_key=True,
        ),
        sa.Column("day_ordinal", sa.Integer(), nullable=True),
        sa.Column("offset_minutes", sa.Integer(), nullable=True),
    )
    op.create_table(
        TABLES[1],
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("day", sa.String(10), nullable=False),
        sa.Column("day_ordinal", sa.Integer(), nullable=False),
        sa.Column(
            "raw_record_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            sa.ForeignKey("garmin_raw_records.id"),
            nullable=True,
        ),
    )
    op.create_index("ix_garmin_day_times_day_id", TABLES[1], ["day", "id"])
    dialect = op.get_context().dialect.name
    for statement in POSTGRESQL_DDL if dialect == "postgresql" else SQLITE_DDL:
        op.execute(statement)


def downgrade() -> None:
    if op.get_context().as_sql:
        raise RuntimeError("Garmin time evidence must be checked before an online downgrade")
    for table in TABLES:
        if op.get_bind().scalar(sa.text(f"SELECT count(*) FROM {table}")):
            raise RuntimeError(f"{table} holds evidence; downgrade refused")
    for table in reversed(TABLES):
        op.drop_table(table)
