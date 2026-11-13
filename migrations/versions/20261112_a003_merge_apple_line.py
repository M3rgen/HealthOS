"""Join the Apple extension (20261111_a002) with the main line (20261111_0033).

A merge revision only: it runs no SQL. This revision belongs to the GitHub-only Apple
extension. The main line has gained ``20261111_0030`` to ``20261111_0033`` since the previous
merge (``20261111_a002``); neither parent's ``down_revision`` is edited. When the main line
gains a later revision, the extension adds another merge revision like this one.
"""

revision = "20261112_a003"
down_revision = ("20261111_0033", "20261111_a002")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """A merge revision: no SQL."""


def downgrade() -> None:
    """A merge revision: no SQL."""
