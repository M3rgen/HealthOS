"""Append completed daily projection outcomes; infer nothing from existing archives."""

import sqlalchemy as sa
from alembic import op

revision = "20261111_0032"
down_revision = "20261111_0031"
branch_labels = None
depends_on = None

TABLE = "garmin_daily_projections"
POSTGRESQL_DDL = (
    f"CREATE TRIGGER {TABLE}_append_only\nBEFORE UPDATE OR DELETE ON {TABLE} "
    "FOR EACH ROW\nEXECUTE FUNCTION healthos_refuse_change()",
    f"CREATE TRIGGER {TABLE}_no_truncate\nBEFORE TRUNCATE ON {TABLE} "
    "FOR EACH STATEMENT\nEXECUTE FUNCTION healthos_refuse_change()",
    f"ALTER TABLE {TABLE} ENABLE ALWAYS TRIGGER {TABLE}_append_only",
    f"ALTER TABLE {TABLE} ENABLE ALWAYS TRIGGER {TABLE}_no_truncate",
)
SQLITE_DDL = tuple(
    f"CREATE TRIGGER {TABLE}_no_{operation.lower()}\nBEFORE {operation} ON {TABLE}\nBEGIN\n"
    f"    SELECT RAISE(ABORT, '{TABLE} keeps every record: {operation} refused');\nEND"
    for operation in ("UPDATE", "DELETE")
)


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("source_record_id", sa.String(160), nullable=False),
        sa.Column("raw_record_ids", sa.JSON(), nullable=False),
        sa.Column("normalized_key", sa.String(255), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_garmin_daily_projections")),
    )
    op.create_index("ix_garmin_daily_projections_source_id", TABLE, ["source_record_id", "id"])
    dialect = op.get_context().dialect.name
    for statement in POSTGRESQL_DDL if dialect == "postgresql" else SQLITE_DDL:
        op.execute(statement)


def downgrade() -> None:
    if op.get_context().as_sql:
        raise RuntimeError("Garmin daily projections must be checked before an online downgrade")
    if op.get_bind().scalar(sa.text(f"SELECT count(*) FROM {TABLE}")):
        raise RuntimeError(f"{TABLE} holds evidence; downgrade refused")
    op.drop_table(TABLE)
