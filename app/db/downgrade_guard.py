"""The one point where a data-losing downgrade is refused (installed by migrations/env.py).

Earlier revisions guard only some of the tables they drop; ``alembic downgrade base`` dropped
``raw_health_data`` and ``manual_measurements`` with rows in them. Migrations are not edited,
so the guard sits on Alembic's own ``drop_table`` and ``drop_column`` operations: a table with
rows is never dropped, and neither is a column holding evidence that nothing else can rebuild.
Dropping an empty table, the only thing a test database downgrade does, is untouched. Upgrades
never drop a table (project rule 3), so they do not reach it.

A refusal rolls back the whole downgrade transaction, except that ``20261017_0007`` commits
partway (its ``DROP INDEX CONCURRENTLY``). Revisions below it are therefore checked before the
first step runs (``refuse_lossy_plan``), so a refused ``base`` changes nothing at all. The lock
taken before counting is the one ``DROP`` takes anyway, so no writer can slip in between.
"""

from alembic import context as alembic_context
from alembic.operations import Operations, ops, toimpl
from alembic.runtime.migration import MigrationContext, RevisionStep
from sqlalchemy import text

from app.db.dialect import database_operation

# Dropped even with rows: rebuilt from the archive (derived_days and Google heart packets),
# their initial-fill cursor (google_heart_projection_state), operational timestamps with
# a row seeded by their own migration (reliability_state), a resumable pull's bookkeeping
# (garmin_pull_state) and one display setting (display_preferences).
DISPOSABLE_TABLES = frozenset(
    {
        "derived_days",
        "google_heart_hours",
        "google_heart_projection_state",
        "display_preferences",
        "garmin_pull_state",
        "reliability_state",
    }
)
# The operator's existing consent, each named by its revision's own guard ("after a backup").
# It covers what that revision drops and nothing else: no flag reaches raw_health_data.
CONSENTED_TABLES = {
    "allow-version-loss": frozenset(
        {"raw_api_responses", "raw_health_data_events", "raw_health_data_versions"}
    ),
    "allow-identity-loss": frozenset(
        {
            "fetch_rounds",
            "identity_deferred_deletions",
            "identity_reconcile_state",
            "raw_health_data_identity",
            "raw_health_data_supersessions",
            "raw_source_agreements",
            "raw_source_chain_events",
            "raw_source_chains",
            "raw_source_decisions",
            "raw_source_devices",
            "raw_source_pairs",
            "raw_source_sightings",
        }
    ),
    "allow-flag-loss": frozenset({"manual_measurement_flag_events"}),
}
# column -> what makes a row's value irreplaceable once the column is gone.
EVIDENCE_COLUMNS = {
    "raw_body": "raw_body IS NOT NULL",
    "is_quarantined": "is_quarantined",
    "quarantine_reason": "quarantine_reason IS NOT NULL",
}

AUTOCOMMIT_REVISION = "20261017_0007"
_INITIAL_TABLES = (
    "active_zone_minutes",
    "backfill_jobs",
    "distance",
    "energy_expenditure",
    "exercise_sessions",
    "heart_rate",
    "heart_rate_variability",
    "oauth_tokens",
    "oxygen_saturation",
    "raw_health_data",
    "respiratory_rate",
    "resting_heart_rate",
    "scalar_metrics",
    "sleep_sessions",
    "steps",
    "sync_state",
    "temperature",
    "vo2_max",
    "webhook_events",
    "weight",
)
_QUARANTINED_TABLES = tuple(
    table
    for table in _INITIAL_TABLES
    if table not in ("backfill_jobs", "oauth_tokens", "sync_state", "webhook_events")
)
# What each revision below the commit point would lose: (table, condition for a loss).
BELOW_AUTOCOMMIT: dict[str, list[tuple[str, str | None]]] = {
    "20261001_0006": [("manual_workouts", None)],
    "20260930_0005": [("manual_measurements", None)],
    "20260818_0003": [
        ("webhook_batch_items", None),
        ("webhook_notifications", None),
        ("webhook_events", EVIDENCE_COLUMNS["raw_body"]),
    ],
    "20260818_0002": [
        (table, condition)
        for table in _QUARANTINED_TABLES
        for condition in (
            EVIDENCE_COLUMNS["is_quarantined"],
            EVIDENCE_COLUMNS["quarantine_reason"],
        )
    ],
    "20260818_0001": [(table, None) for table in _INITIAL_TABLES],
}


def refuse_lossy_plan(context: MigrationContext) -> None:
    """Before anything runs: refuse a plan that would commit a part and then lose rows."""
    # Alembic's own plan for this command; empty (or all upgrades) for everything else.
    plan = context.opts.get("fn")
    if plan is None:
        return
    steps = plan(context.get_current_heads(), context)
    # A stamp moves the version row only: it has no revision to run and loses nothing.
    revisions = {
        step.revision.revision
        for step in steps
        if isinstance(step, RevisionStep) and step.is_downgrade
    }
    if AUTOCOMMIT_REVISION not in revisions:
        return  # One transaction: the guards below still roll all of it back.
    bind = context.connection
    if bind is None:
        return  # offline: the op-level guards write their refusal into the script
    for revision, losses in BELOW_AUTOCOMMIT.items():
        if revision not in revisions:
            continue
        for table, where in losses:
            condition = f" WHERE {where}" if where else ""
            if bind.scalar(text(f"SELECT 1 FROM {table}{condition} LIMIT 1")) is not None:
                what = f"{table} holds rows" if where is None else f"{table} holds values"
                raise RuntimeError(_refused(f"{what} ({revision})"))


def _refused(what: str) -> str:
    return f"{what}; downgrade refused: it would lose data that cannot be rebuilt"


def _quoted(operations: Operations, table: str, schema: str | None) -> str:
    quote = operations.migration_context.impl.dialect.identifier_preparer.quote
    return f"{quote(schema)}.{quote(table)}" if schema else quote(table)


@database_operation
def _refuse_while_held(
    operations: Operations, table: str, schema: str | None, where: str | None, what: str
) -> None:
    context = operations.migration_context
    target = _quoted(operations, table, schema)
    condition = f" WHERE {where}" if where else ""
    if context.as_sql:
        # The script cannot be checked now; it refuses when it runs, before the drop.
        operations.execute(f"LOCK TABLE {target} IN ACCESS EXCLUSIVE MODE")
        operations.execute(
            f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM {target}{condition}) THEN "
            f"RAISE EXCEPTION '{_refused(what)}'; END IF; END $$"
        )
        return
    bind = operations.get_bind()
    bind.execute(text(f"LOCK TABLE {target} IN ACCESS EXCLUSIVE MODE"))
    if bind.scalar(text(f"SELECT 1 FROM {target}{condition} LIMIT 1")) is not None:
        raise RuntimeError(_refused(what))


def _consented(table: str) -> bool:
    arguments = alembic_context.get_x_argument(as_dictionary=True)
    return any(
        table in tables and arguments.get(flag) == "1" for flag, tables in CONSENTED_TABLES.items()
    )


def _drop_table(operations: Operations, operation: ops.DropTableOp) -> None:
    if operation.table_name not in DISPOSABLE_TABLES and not _consented(operation.table_name):
        _refuse_while_held(
            operations,
            operation.table_name,
            operation.schema,
            None,
            f"{operation.table_name} holds rows",
        )
    toimpl.drop_table(operations, operation)


def _drop_column(operations: Operations, operation: ops.DropColumnOp) -> None:
    where = EVIDENCE_COLUMNS.get(operation.column_name)
    if where is not None:
        _refuse_while_held(
            operations,
            operation.table_name,
            operation.schema,
            where,
            f"{operation.table_name}.{operation.column_name} holds values",
        )
    toimpl.drop_column(operations, operation)


def install() -> None:
    """Replace Alembic's two destructive operations for the rest of this process."""
    Operations.implementation_for(ops.DropTableOp, replace=True)(_drop_table)
    Operations.implementation_for(ops.DropColumnOp, replace=True)(_drop_column)
