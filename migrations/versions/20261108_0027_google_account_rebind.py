"""Preserve deliberate Google account changes as immutable evidence."""

import sqlalchemy as sa
from alembic import op

revision = "20261108_0027"
down_revision = "20261108_0026"
branch_labels = None
depends_on = None

TABLE = "google_account_rebind_events"
POSTGRESQL_DDL = (
    f"CREATE TRIGGER {TABLE}_append_only\nBEFORE UPDATE OR DELETE ON {TABLE} FOR EACH ROW\n"
    "EXECUTE FUNCTION healthos_refuse_change()",
    f"CREATE TRIGGER {TABLE}_no_truncate\nBEFORE TRUNCATE ON {TABLE} FOR EACH STATEMENT\n"
    "EXECUTE FUNCTION healthos_refuse_change()",
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
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "credential_version_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            sa.ForeignKey("oauth_credential_versions.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("old_health_user_id", sa.String(128), nullable=False),
        sa.Column("new_health_user_id", sa.String(128), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    dialect = op.get_bind().dialect.name
    for statement in POSTGRESQL_DDL if dialect == "postgresql" else SQLITE_DDL:
        op.execute(sa.text(statement))


def downgrade() -> None:
    if op.get_context().as_sql:
        raise RuntimeError("Google account changes must be checked before an online downgrade")
    if op.get_bind().scalar(sa.text(f"SELECT count(*) FROM {TABLE}")):
        raise RuntimeError("Google account changes hold history; downgrade refused")
    op.drop_table(TABLE)
