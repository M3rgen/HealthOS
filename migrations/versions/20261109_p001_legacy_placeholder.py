"""A distinct, empty bridge for databases stamped by v0.2.2.

The old main-side placeholder borrowed the real Apple extension's ``20261107_a001``
identity. It now has its own identity, so merging the extension cannot replace a no-op
with schema-changing code under an already-applied revision.

``legacy_revision`` lets migrations/env.py translate that old stored stamp only when
the actual revision is absent from this checkout. Only Alembic's bookkeeping changes;
all existing extension tables, rows and guards stay in place. If the extension is
installed, its real revision runs normally and is never translated or skipped.

Both directions are empty: this bridge never installs or removes extension tables.
"""

revision = "20261109_p001"
legacy_revision = "20261107_a001"
down_revision = "20261107_0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """No schema or record changes."""


def downgrade() -> None:
    """No schema or record changes."""
