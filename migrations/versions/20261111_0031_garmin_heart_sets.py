"""Append daily heart normalization sets; no existing rows are changed or inferred."""

import sqlalchemy as sa
from alembic import op

revision = "20261111_0031"
down_revision = "20261111_0030"
branch_labels = None
depends_on = None

TABLES = ("garmin_heart_sets", "garmin_heart_members")
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
    identifier = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
    op.create_table(
        TABLES[0],
        sa.Column("id", identifier, autoincrement=True, nullable=False),
        sa.Column("source_record_id", sa.String(160), nullable=False),
        sa.Column("raw_record_id", identifier, nullable=False),
        sa.Column("members_sha256", sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_garmin_heart_sets")),
        sa.ForeignKeyConstraint(
            ["raw_record_id"],
            ["garmin_raw_records.id"],
            name=op.f("fk_garmin_heart_sets_raw_record_id_garmin_raw_records"),
        ),
    )
    op.create_index("ix_garmin_heart_sets_source_id", TABLES[0], ["source_record_id", "id"])
    op.create_table(
        TABLES[1],
        sa.Column("set_id", identifier, nullable=False),
        sa.Column("sample_key", sa.String(255), nullable=False),
        sa.PrimaryKeyConstraint("set_id", "sample_key", name=op.f("pk_garmin_heart_members")),
        sa.ForeignKeyConstraint(
            ["set_id"],
            ["garmin_heart_sets.id"],
            name=op.f("fk_garmin_heart_members_set_id_garmin_heart_sets"),
        ),
    )
    dialect = op.get_context().dialect.name
    for statement in POSTGRESQL_DDL if dialect == "postgresql" else SQLITE_DDL:
        op.execute(statement)


def downgrade() -> None:
    if op.get_context().as_sql:
        raise RuntimeError("Garmin heart sets must be checked before an online downgrade")
    for table in TABLES:
        if op.get_bind().scalar(sa.text(f"SELECT count(*) FROM {table}")):
            raise RuntimeError(f"{table} holds evidence; downgrade refused")
    for table in reversed(TABLES):
        op.drop_table(table)
