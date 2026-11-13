"""Track normalization independently of successful Garmin HTTP pulls."""

import sqlalchemy as sa
from alembic import op

revision = "20261112_0034"
down_revision = "20261111_0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "garmin_normalization_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(40), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_garmin_normalization_state")),
        sa.CheckConstraint("id = 1", name=op.f("ck_garmin_normalization_state_singleton")),
        sa.CheckConstraint(
            "last_error_code IS NULL OR last_error_code = 'garmin_normalization_failed'",
            name=op.f("ck_garmin_normalization_state_safe_error_code"),
        ),
    )


def downgrade() -> None:
    op.drop_table("garmin_normalization_state")
