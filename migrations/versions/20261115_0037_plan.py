"""Add private, immutable plan versions and a replaceable singleton UI state.

Only new tables and guards; no existing records or tables are read or altered.
Frozen definitions, independent of runtime models. Populated tables cannot be downgraded,
and an offline (--sql) downgrade is refused because it cannot check them.
"""

import sqlalchemy as sa
from alembic import op

revision = "20261115_0037"
down_revision = "20261114_0036"
branch_labels = None
depends_on = None

POSTGRESQL_DDL = (
    "CREATE TRIGGER plan_versions_append_only\nBEFORE UPDATE OR DELETE ON plan_versions "
    "FOR EACH ROW\nEXECUTE FUNCTION healthos_refuse_change()",
    "CREATE TRIGGER plan_versions_no_truncate\nBEFORE TRUNCATE ON plan_versions "
    "FOR EACH STATEMENT\nEXECUTE FUNCTION healthos_refuse_change()",
    "ALTER TABLE plan_versions ENABLE ALWAYS TRIGGER plan_versions_append_only",
    "ALTER TABLE plan_versions ENABLE ALWAYS TRIGGER plan_versions_no_truncate",
)
SQLITE_DDL = tuple(
    f"CREATE TRIGGER plan_versions_no_{operation.lower()}\nBEFORE {operation} ON plan_versions\n"
    f"BEGIN\n    SELECT RAISE(ABORT, 'plan_versions keeps every record: {operation} refused');\nEND"
    for operation in ("UPDATE", "DELETE")
)


def upgrade() -> None:
    op.create_table(
        "plan_versions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column(
            "imported_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.CheckConstraint("schema_version = 1", name=op.f("ck_plan_versions_schema_version")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plan_versions")),
        sa.UniqueConstraint("content_sha256", name=op.f("uq_plan_versions_content_sha256")),
    )
    op.create_table(
        "plan_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("checked_item_ids", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("id = 1", name=op.f("ck_plan_state_singleton")),
        sa.CheckConstraint("schema_version = 1", name=op.f("ck_plan_state_schema_version")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plan_state")),
    )
    dialect = op.get_context().dialect.name
    for statement in POSTGRESQL_DDL if dialect == "postgresql" else SQLITE_DDL:
        op.execute(statement)


def downgrade() -> None:
    if op.get_context().as_sql:
        raise RuntimeError("Plan tables must be checked before an online downgrade")
    # Lock before counting; the shared drop guard also checks each table independently.
    if op.get_context().dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")
        op.execute("LOCK TABLE plan_versions, plan_state IN ACCESS EXCLUSIVE MODE")
    for table in ("plan_versions", "plan_state"):
        if op.get_bind().scalar(sa.text(f"SELECT 1 FROM {table} LIMIT 1")) is not None:
            raise RuntimeError(f"{table} holds rows; downgrade refused")
    op.drop_table("plan_state")
    op.drop_table("plan_versions")
