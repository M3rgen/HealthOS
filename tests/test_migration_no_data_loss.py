"""The non-negotiable: no upgrade removes rows or columns (CONTRIBUTING.md, rule 2)."""

from __future__ import annotations

import re

from alembic import command
from alembic.config import Config

from app.core.config import get_settings

# Recreating a view or index, or replacing a trigger, keeps the data; these do not.
# Statement starts only: trigger definitions mention TRUNCATE and DELETE by design.
LOSES_DATA = re.compile(
    r"^\s*(?:DROP\s+TABLE\b|TRUNCATE\b|DELETE\s+FROM\b|ALTER\s+TABLE\s+\S+\s+DROP\s+COLUMN\b)",
    re.IGNORECASE | re.MULTILINE,
)
# A CREATE FUNCTION body runs later, when its trigger fires; a DO block runs during the upgrade
# and stays in the scan.
FUNCTION_BODY = re.compile(
    r"(CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\b[^;$]*?\bAS\s+)(\$\w*\$)(.*?)\2",
    re.IGNORECASE | re.DOTALL,
)
# The one removal a trigger body may perform: the committing transaction's own entries in the
# invalidation queue (0028). The queue holds no record; its rows never outlive the transaction.
ALLOWED_IN_FUNCTIONS = {
    "DELETE FROM derived_day_pending WHERE xact = pg_current_xact_id()::text::bigint;"
}

# Alembic's own bookkeeping for the one merge revision (0029): it replaces two head rows of
# alembic_version with one. Not a record; exactly one such statement is allowed, nothing else.
MERGE_BOOKKEEPING = re.compile(
    r"^DELETE FROM alembic_version WHERE alembic_version\.version_num = '\w+';$", re.MULTILINE
)


def split_function_bodies(ddl: str) -> tuple[str, list[str]]:
    bodies = [match.group(3) for match in FUNCTION_BODY.finditer(ddl)]
    return FUNCTION_BODY.sub(r"\1", ddl), bodies


def removals_in(body: str) -> set[str]:
    return {
        body[match.end() - len(match.group(0).lstrip()) :].splitlines()[0].strip()
        for match in LOSES_DATA.finditer(body)
    }


def test_the_scan_still_sees_what_an_upgrade_would_run():
    function = "CREATE FUNCTION f() RETURNS trigger LANGUAGE plpgsql AS $$\nDELETE FROM a;\n$$;\n"
    statements, bodies = split_function_bodies(function)
    assert LOSES_DATA.findall(statements) == [] and removals_in(bodies[0]) == {"DELETE FROM a;"}
    do_block = "DO $$\nBEGIN\nDELETE FROM raw_health_data;\nEND\n$$;\n"
    assert LOSES_DATA.findall(split_function_bodies(do_block)[0])
    assert LOSES_DATA.findall(split_function_bodies(function + "DELETE FROM b;\n")[0])


def test_upgrading_the_whole_chain_generates_no_statement_that_loses_data(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://health:health@postgres:5432/health")
    get_settings.cache_clear()
    try:
        command.upgrade(Config("alembic.ini"), "head", sql=True)
        ddl = capsys.readouterr().out
    finally:
        get_settings.cache_clear()
    assert "CREATE TABLE" in ddl  # the chain really was generated
    statements, bodies = split_function_bodies(ddl)
    assert bodies  # the triggers really were generated
    statements, merges = MERGE_BOOKKEEPING.subn("", statements)
    assert merges == 3  # 0029 and the Apple extension's merges, as Alembic's offline plan runs them
    assert LOSES_DATA.findall(statements) == []
    assert set().union(*map(removals_in, bodies)) <= ALLOWED_IN_FUNCTIONS
