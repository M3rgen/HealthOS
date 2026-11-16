"""Add the disposable, method-versioned FIT track packages; no source evidence changes."""

import sqlalchemy as sa
from alembic import op

revision = "20261115_0039"
down_revision = "20261115_0038"
branch_labels = None
depends_on = None
TABLE = "garmin_tracks"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("source_sha256", sa.String(64), primary_key=True),
        sa.Column("method_version", sa.Integer(), primary_key=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("samples", sa.LargeBinary(), nullable=False),
        sa.Column("laps", sa.LargeBinary(), nullable=False),
        sa.Column("events", sa.LargeBinary(), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.CheckConstraint("method_version > 0 AND sample_count >= 0", name="valid_counts"),
        sa.CheckConstraint("status IN ('ready', 'invalid')", name="valid_status"),
    )


def downgrade() -> None:
    # Rebuilt from garmin_activity_files and manual FIT rows, which this never touches;
    # the downgrade guard lists the table as disposable (dropped even with rows).
    op.drop_table(TABLE)
