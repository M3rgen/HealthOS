"""Exercise PostgreSQL verification without a server: table names confer no exemptions."""

from unittest.mock import AsyncMock, Mock

import pytest

from app.db.guards import definitions
from app.services.archive import raw_history_verification as verification


@pytest.mark.parametrize("table", ["apple_payloads", "apple_raw_records", "elsewhere"])
async def test_shared_protective_function_on_unregistered_table_is_reported(monkeypatch, table):
    _, functions, triggers = definitions()
    if any(relation == table for relation, _, _ in triggers):
        pytest.skip("registered here: the GitHub-only Apple extension guards its own tables")
    expected = {
        (relation, trigger): f"CREATE TRIGGER {trigger} ON {relation} synthetic"
        for relation, trigger, _ in triggers
    }
    rows = [
        (relation, trigger, "A", function, functions[function], expected[(relation, trigger)])
        for relation, trigger, function in triggers
    ]
    rows.append((table, "foreign_guard", "A", "healthos_refuse_change", "", "synthetic"))
    result = Mock()
    result.tuples.return_value = rows
    session = Mock(execute=AsyncMock(return_value=result))
    monkeypatch.setattr(verification, "_expected_pg_definitions", AsyncMock(return_value=expected))
    assert await verification.verify_trigger_definitions.__wrapped__(session) == [
        f"protective trigger foreign_guard sits on {table}, which is not a guarded table "
        "(was a guarded table renamed or replaced?)"
    ]
