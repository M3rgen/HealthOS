"""Small partial indexes for the history readers, built ``CONCURRENTLY`` (no data changes).

Cold, the past-week, month, coverage and step-history views spent most of their time on
random reads that a handful of small indexes avoid (measured on a full-size copy, issue 756):

* ``ix_raw_health_data_long_steps``: the few step intervals longer than a day (or with a
  bound missing, infinite or outside the years 1-9999: only bounds within them are
  subtracted, so no row's write can fail on the predicate). A step window read through
  ``(data_type, COALESCE(end, start))`` was bounded from below only, so an older window
  fetched every later step row up to today. Without the long ones the rest of a window ends
  within a day after it: a closed range of that index (``raw_step_window_parts``).
* ``ix_raw_health_data_supersessions_type_keys``: the links by data type and old key, with
  what the in-force test and the holder lookup need (an index-only probe), so a reader's
  probes stay on its type's pages; ``(old_google_data_point_id, data_type)`` and the primary
  key spread them over every type's links. Heart rate (nine tenths of the links, read through
  its own anti-join) is left out.
* ``ix_heart_rate_provider_time``, ``ix_steps_provider_record``,
  ``ix_oxygen_saturation_provider_record``, ``ix_heart_rate_variability_provider_record``:
  the non-Google rows (Garmin, FIT, sample data), a small share of these tables, which the
  provider readers otherwise find by scanning every Google row of a day or the whole table.

On PostgreSQL each index is built ``CONCURRENTLY`` in its own non-transactional step: writers
(webhooks, the scheduler) are never blocked, the table is read twice, and the build waits for
transactions that started before it (a running dump: do not deploy while the nightly backup
runs, docs/MIGRATIONS.md). No lock or statement timeout applies to the build. A build that
stops half way (the process killed, a deadlock, a cancel) leaves an invalid index behind:
when one with these names is found, valid ones are kept, an invalid one is dropped
(``DROP INDEX CONCURRENTLY``; an index holds no data) and built again. A failed build is
retried; after the last attempt its invalid leftover is dropped and the upgrade fails,
changing nothing else: running it again starts that index anew.

A build waits for every transaction older than itself, a dump's included, and cannot be
cancelled safely. So none is started while another client session of the database holds a
transaction or snapshot open (autovacuum does not count: a build does not wait for it): the
upgrade waits up to ten seconds for a moment when none does (the application's are short),
else leaves the remaining indexes for later with a WARNING that prints their statements, one
per line, and goes on. Without the long-step index the step reader keeps the old
one-sided window (``google_daily_readers._step_window_parts``), so nothing is slower than
before; ``healthos verify`` names every missing or invalid index of this revision.

Last, statistics on the long-step expression (``raw_health_data_long_steps``) and one
``ANALYZE raw_health_data``: without them the planner guesses a third of the step rows are
long and may plan the long part as a scan of the raw and step tables.

downgrade() drops the indexes and the statistics in its transaction (5 s lock timeout).
"""

import time

import sqlalchemy as sa
from alembic import context, op

revision = "20261116_0041"
down_revision = "20261115_0040"
branch_labels = None
depends_on = None

ATTEMPTS = 3
BUSY_WAIT = 10.0
# Client sessions only: a concurrent build does not wait for autovacuum or a plain VACUUM
# (PostgreSQL skips them when it waits for older snapshots), so neither keeps it from starting.
BUSY_SQL = (
    "SELECT count(*) FROM pg_stat_activity a WHERE a.pid <> pg_backend_pid() "
    "AND a.datname = current_database() AND a.backend_type = 'client backend' "
    "AND (a.backend_xid IS NOT NULL OR a.backend_xmin IS NOT NULL)"
)
# name, table, key columns, INCLUDE columns (PostgreSQL), predicate. The long steps first:
# without it the step reader keeps the old one-sided window; then the smallest builds.
INDEXES = (
    (
        "ix_raw_health_data_long_steps",
        "raw_health_data",
        ("source_start_time",),
        (),
        "CASE WHEN data_type <> 'steps' THEN false WHEN source_start_time >= "
        "'0001-01-01 00:00:00+00'::timestamptz AND source_start_time < "
        "'10000-01-01 00:00:00+00'::timestamptz AND source_end_time >= "
        "'0001-01-01 00:00:00+00'::timestamptz AND source_end_time < "
        "'10000-01-01 00:00:00+00'::timestamptz THEN (source_end_time - source_start_time) > "
        "interval '1 day' ELSE true END",
    ),
    (
        "ix_steps_provider_record",
        "steps",
        ("provider", "source_record_id"),
        (),
        "provider <> 'GOOGLE'",
    ),
    (
        "ix_heart_rate_variability_provider_record",
        "heart_rate_variability",
        ("provider", "source_record_id"),
        (),
        "provider <> 'GOOGLE'",
    ),
    (
        "ix_oxygen_saturation_provider_record",
        "oxygen_saturation",
        ("provider", "source_record_id"),
        (),
        "provider <> 'GOOGLE'",
    ),
    (
        "ix_raw_health_data_supersessions_type_keys",
        "raw_health_data_supersessions",
        ("data_type", "old_google_data_point_id"),
        ("new_google_data_point_id", "old_fingerprint", "new_fingerprint"),
        "data_type <> 'heart-rate'",
    ),
    (
        "ix_heart_rate_provider_time",
        "heart_rate",
        ("provider", "timestamp"),
        (),
        "provider <> 'GOOGLE'",
    ),
)
# Statistics on the long-step expression: without them the planner guesses that a third of
# the step rows are long, plans the long part for tens of thousands of rows and may then scan
# the raw and step tables whole. ANALYZE right after: autovacuum analyzes this table only
# after a tenth of it has changed (weeks), and until then the guess would stand.
STATISTICS = "raw_health_data_long_steps"
# SQLite (tests only) has no interval type: there the index covers every step row.
SQLITE_WHERE = {"ix_raw_health_data_long_steps": "data_type = 'steps'"}


def create_statement(name: str, table: str, columns, include, where: str) -> str:
    covering = f" INCLUDE ({', '.join(include)})" if include else ""
    return (
        f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {table} "
        f"({', '.join(columns)}){covering} WHERE {where}"
    )


def validity(bind, name: str) -> bool | None:
    """``True`` valid, ``False`` left invalid by an interrupted build, ``None`` absent."""
    return bind.scalar(
        sa.text(
            "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = current_schema() AND c.relname = :name"
        ),
        {"name": name},
    )


def drop_invalid(bind, name: str) -> None:
    if validity(bind, name) is False:
        print(f"{revision}: dropping the invalid {name} an interrupted build left behind.")
        bind.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {name}"))


def build(bind, name: str, statement: str, attempts: int = ATTEMPTS) -> None:
    """Build ``name`` unless a valid one exists; never keep an invalid one."""
    for attempt in range(1, attempts + 1):
        if validity(bind, name) is True:
            return
        drop_invalid(bind, name)
        print(f"{revision}: building {name} concurrently (attempt {attempt} of {attempts}).")
        try:
            bind.execute(sa.text(statement))
        except sa.exc.DBAPIError as exc:
            drop_invalid(bind, name)
            if attempt == attempts:
                raise RuntimeError(
                    f"{revision}: {name} could not be built ({type(exc.orig).__name__}); its "
                    "invalid leftover was dropped and nothing else changed. Run the upgrade "
                    "again, outside the nightly backup."
                ) from exc
            print(f"{revision}: building {name} failed ({type(exc.orig).__name__}); retrying.")


def quiet(bind, wait: float = BUSY_WAIT, poll: float = 0.5) -> int:
    """How many other sessions of this database hold a transaction or snapshot open, after
    waiting up to ``wait`` seconds for none to (an application's are short, a dump's not)."""
    deadline = time.monotonic() + wait
    while True:
        busy = bind.scalar(sa.text(BUSY_SQL)) or 0
        if not busy or time.monotonic() >= deadline:
            return busy
        time.sleep(poll)


def build_all(bind, indexes=INDEXES) -> list[str]:
    """Build every index that is not valid yet; return the statements left for later, one
    statement each (``psql`` runs a multi-statement ``-c`` in one transaction, which
    ``CONCURRENTLY`` refuses)."""
    later: list[str] = []
    for index in indexes:
        name, statement = index[0], create_statement(*index)
        if validity(bind, name) is True:
            continue
        # A build waits for every transaction older than it, a dump's to its end, and a
        # cancelled build leaves an invalid index: none is started while one is open.
        if later or quiet(bind):  # one wait in all: the long steps' index comes first
            later += [f"DROP INDEX CONCURRENTLY IF EXISTS {name};", f"{statement};"]
            continue
        build(bind, name, statement)
    if later:
        print(
            f"WARNING {revision}: indexes left for later: another client session held a "
            "transaction or snapshot open (a dump's?). The upgrade goes on without them; the "
            "history views stay as slow as before, and `healthos verify` names each one until "
            'it is built. When no backup runs, run each line below on its own (psql -c "<line>": '
            "the statements contain single quotes; or interactive psql):\n" + "\n".join(later)
        )
    return later


def statistics_statements() -> list[str]:
    long_steps = next(index for index in INDEXES if index[0] == "ix_raw_health_data_long_steps")
    return [
        f"CREATE STATISTICS IF NOT EXISTS {STATISTICS} ON (({long_steps[4]})) FROM raw_health_data",
        "ANALYZE raw_health_data",
    ]


def statistics(bind) -> None:
    """Create the long-step statistics and gather them at once (a sample of the table; reads
    and writers go on, only another ANALYZE or VACUUM of the table waits)."""
    print(f"{revision}: analyzing raw_health_data for the long-step statistics.")
    for statement in statistics_statements():
        bind.execute(sa.text(statement))


def upgrade() -> None:
    if op.get_context().dialect.name != "postgresql":
        for name, table, columns, _include, where in INDEXES:
            keys = [sa.text(column) if "(" in column else column for column in columns]
            op.create_index(name, table, keys, sqlite_where=sa.text(SQLITE_WHERE.get(name, where)))
        return
    with op.get_context().autocommit_block():
        if context.is_offline_mode():
            for index in INDEXES:
                op.execute(create_statement(*index))
            for statement in statistics_statements():
                op.execute(statement)
            return
        bind = op.get_bind()
        # No timeout of any kind (a role's or database's included): a cancelled build
        # leaves an invalid index.
        bind.execute(sa.text("SET lock_timeout = 0"))
        bind.execute(sa.text("SET statement_timeout = 0"))
        try:
            build_all(bind)
            statistics(bind)
        finally:
            bind.execute(sa.text("RESET statement_timeout"))
            bind.execute(sa.text("RESET lock_timeout"))


def downgrade() -> None:
    if op.get_context().dialect.name != "postgresql":
        for name, table, *_ in reversed(INDEXES):
            op.drop_index(name, table_name=table)
        return
    # In the downgrade's transaction, so a later refusal keeps them; a writer's lock is waited
    # for at most 5 s (the drop is a moment's exclusive lock), then nothing changes.
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(f"DROP STATISTICS IF EXISTS {STATISTICS}")
    for name, *_ in reversed(INDEXES):
        op.execute(f"DROP INDEX IF EXISTS {name}")
