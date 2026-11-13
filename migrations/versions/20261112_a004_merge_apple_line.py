"""Join the Apple extension (20261112_a003) with the main line (20261112_0034).

A merge revision only: it runs no SQL. This revision belongs to the GitHub-only Apple
extension; neither parent's ``down_revision`` is edited. When the main line gains a later
revision, the extension adds another merge revision like this one.
"""

revision = "20261112_a004"
down_revision = ("20261112_0034", "20261112_a003")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """A merge revision: no SQL."""


def downgrade() -> None:
    """A merge revision: no SQL."""
