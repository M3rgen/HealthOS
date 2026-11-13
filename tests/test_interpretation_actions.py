"""Every interpretation action remains available through the injected archive reader."""

import json

import pytest
from interpretation_records import QUERY

from app.services.interpretation.interpretation_types import ACTIONS, Interpretation
from app.services.umay.codex_health import HEALTH_TOOL, read_health


@pytest.mark.parametrize("action", sorted(ACTIONS))
async def test_new_actions_are_advertised_and_return_typed_complete_json(sessions, action):
    arguments = dict(QUERY, action=action)
    if action == "relationship":
        arguments.update(metric="resting_bpm", other_metric="hrv_ms", lags=[0, 1])
    assert action in HEALTH_TOOL["inputSchema"]["properties"]["action"]["enum"]
    result = await read_health(sessions, arguments)
    assert result["schema_version"] == "2" and "error" not in result
    assert Interpretation.model_validate(result).action == action
    json.dumps(result, allow_nan=False)
