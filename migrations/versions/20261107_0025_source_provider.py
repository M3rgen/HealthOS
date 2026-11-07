"""Provider of every normalized row (app/core/providers.py, app/models/health.py).

Two columns on each of the fifteen normalized tables: ``provider`` (``GOOGLE`` or
``GARMIN``; every existing row is ``GOOGLE`` through the column default, nothing is
rewritten) and ``source_record_id`` (the provider's own record key of a non-Google row;
NULL for Google rows, whose key is ``google_data_point_id``).

Only ``ADD COLUMN``: on PostgreSQL a column with a constant default is a catalog change
(no table rewrite, a momentary lock per table); on SQLite a native ``ADD COLUMN`` keeps
the tables' triggers (0018/0020) and the ``analytics_*`` views (0012/0013), which a
table rebuild (``batch_alter_table``, needed to drop a NOT NULL) would not: measured
before this migration was written, the rebuild of ``steps`` fails on the view that
references it. ``google_data_point_id`` therefore keeps its NOT NULL and its unique
index: a Garmin row stores its ``garmin:<kind>:<key>`` record key there, which no Google
id can equal. No index is added. No existing row is read or changed.

downgrade() drops the two columns and refuses while any table holds a non-Google row
(its provider would be lost and the row would pass for a Google row without a raw record).
"""

import sqlalchemy as sa
from alembic import op

revision = "20261107_0025"
down_revision = "20261107_0024"
branch_labels = None
depends_on = None

GOOGLE = "GOOGLE"
# The normalized tables (app/models/health.py), every one keyed by google_data_point_id.
TABLES = (
    "active_zone_minutes",
    "distance",
    "energy_expenditure",
    "exercise_sessions",
    "heart_rate",
    "heart_rate_variability",
    "oxygen_saturation",
    "respiratory_rate",
    "resting_heart_rate",
    "scalar_metrics",
    "sleep_sessions",
    "steps",
    "temperature",
    "vo2_max",
    "weight",
)
COLUMNS = ("provider", "source_record_id")


def upgrade() -> None:
    for table in TABLES:
        op.add_column(
            table,
            sa.Column("provider", sa.String(16), nullable=False, server_default=GOOGLE),
        )
        op.add_column(table, sa.Column("source_record_id", sa.String(512), nullable=True))


def downgrade() -> None:
    if op.get_context().as_sql:
        raise RuntimeError("provider columns must be checked before an online downgrade")
    bind = op.get_bind()
    for table in TABLES:
        held = bind.scalar(
            sa.text(f"SELECT count(*) FROM {table} WHERE provider <> :google"), {"google": GOOGLE}
        )
        if held:
            raise RuntimeError(f"{table} holds {held} non-Google row(s); downgrade refused")
    for table in TABLES:
        for column in reversed(COLUMNS):
            op.drop_column(table, column)
