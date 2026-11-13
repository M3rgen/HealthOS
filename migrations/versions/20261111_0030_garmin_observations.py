"""Append-only Garmin record observations, without rewriting or inferring old evidence.

A successful page links each observed raw version to the response that returned it, in the
same transaction. Reappearing content can thus become current without duplicating its body.
Existing keys keep the legacy max-id rule until they receive an observation. Frozen DDL,
independent of model declarations; downgrade refuses to discard observation evidence.
"""

import sqlalchemy as sa
from alembic import op

revision = "20261111_0030"
down_revision = "20261109_0029"
branch_labels = None
depends_on = None

TABLE = "garmin_record_observations"
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
    identifier = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
    op.create_table(
        TABLE,
        sa.Column("id", identifier, autoincrement=True, nullable=False),
        sa.Column("raw_record_id", identifier, nullable=False),
        sa.Column("response_id", identifier, nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{TABLE}")),
        sa.ForeignKeyConstraint(
            ["raw_record_id"],
            ["garmin_raw_records.id"],
            name=op.f(f"fk_{TABLE}_raw_record_id_garmin_raw_records"),
        ),
        sa.ForeignKeyConstraint(
            ["response_id"],
            ["garmin_api_responses.id"],
            name=op.f(f"fk_{TABLE}_response_id_garmin_api_responses"),
        ),
        sa.UniqueConstraint("response_id", "raw_record_id", name="uq_garmin_observations_response"),
    )
    op.create_index(
        "ix_garmin_observations_record_seen", TABLE, ["raw_record_id", "observed_at", "id"]
    )
    dialect = op.get_context().dialect.name
    for statement in POSTGRESQL_DDL if dialect == "postgresql" else SQLITE_DDL:
        op.execute(statement)


def downgrade() -> None:
    if op.get_context().as_sql:
        raise RuntimeError("Garmin observations must be checked before an online downgrade")
    if op.get_bind().scalar(sa.text(f"SELECT count(*) FROM {TABLE}")):
        raise RuntimeError(f"{TABLE} holds evidence; downgrade refused")
    op.drop_table(TABLE)
