"""Join the Apple extension (20261107_a001) with the main line (20261109_0029).

A merge revision only: it runs no SQL. This revision belongs to the GitHub-only Apple
extension. ``20261107_a001`` follows ``20261107_0025``; the main line has since gained
``20261108_0026`` to ``20261109_0029`` (including the empty bridge ``20261109_p001``, which
never stands in for the real ``20261107_a001`` present here). Neither parent's
``down_revision`` is edited; when the main line gains a later revision, the extension adds
another merge revision like this one. No application schema changes.
"""

revision = "20261111_a002"
down_revision = ("20261109_0029", "20261107_a001")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """A merge revision: no SQL."""


def downgrade() -> None:
    """A merge revision: no SQL."""
