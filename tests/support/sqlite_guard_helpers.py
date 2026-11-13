"""Test-side translation of backend-neutral append-only declarations."""

from app.db.guards import GuardSet


def append_only(guards: GuardSet) -> dict[str, tuple[str, str]]:
    triggers = {}
    for table in guards.immutable_tables:
        for operation in ("UPDATE", "DELETE"):
            name = f"{table}_no_{operation.lower()}"
            triggers[name] = (
                table,
                f"CREATE TRIGGER {name}\nBEFORE {operation} ON {table}\nBEGIN\n"
                f"    SELECT RAISE(ABORT, '{table} keeps every record: {operation} refused');\nEND",
            )
    return triggers
