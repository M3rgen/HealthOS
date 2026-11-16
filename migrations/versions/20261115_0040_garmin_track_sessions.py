"""Link FIT workouts to the uploaded content that produced them; no source evidence changes."""

import sqlalchemy as sa
from alembic import op

revision = "20261115_0040"
down_revision = "20261115_0039"
branch_labels = None
depends_on = None
TABLE = "garmin_track_sessions"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("source_record_id", sa.String(512), primary_key=True),
        sa.Column("source_sha256", sa.String(64), primary_key=True),
    )
    op.create_index("ix_garmin_track_sessions_source", TABLE, ["source_sha256"])


def downgrade() -> None:
    # Derived again from the manual FIT rows by ``--rebuild``; listed as disposable in the
    # downgrade guard (dropped even with rows). The originals are never touched.
    op.drop_index("ix_garmin_track_sessions_source", table_name=TABLE)
    op.drop_table(TABLE)
