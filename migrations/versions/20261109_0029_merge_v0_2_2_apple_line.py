"""Join the legacy placeholder (20261109_p001) with the main line (20261108_0028).

A merge revision only: it runs no SQL. Both parents are the two heads a database can be on:
``20261108_0028`` (installs of ``main``) and ``20261109_p001`` (the distinct bridge for
v0.2.2 stamps, see that revision's module docstring). The merge identity is unchanged,
so databases already at this merge stay at head. No application schema changes.
"""

revision = "20261109_0029"
down_revision = ("20261108_0028", "20261109_p001")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """A merge revision: no SQL."""


def downgrade() -> None:
    """A merge revision: no SQL."""
