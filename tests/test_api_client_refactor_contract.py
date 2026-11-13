"""The size refactor keeps public query/CLI contracts and shared fetch accounting."""

import ast
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import typer.rich_utils
from support.coverage_fixtures import PATH, QUERY
from support.coverage_fixtures import api as api
from typer.main import get_command
from typer.testing import CliRunner

from app.api.sync import coverage_router
from app.cli import main as cli
from app.core.fetch_window import FetchWindow
from app.google_health.client import InvalidPageTokenError, Page
from app.google_health.data_types import REGISTRY
from app.google_health.pagination import PageBudget, PaginationOptions, iter_pages
from app.services.providers.ingestion import GoogleSource

ROOT = Path(__file__).resolve().parents[1]
MODULES = (
    "app/api/codex.py",
    "app/api/sync.py",
    "app/cli/identity_commands.py",
    "app/garmin/client.py",
    "app/google_health/pagination.py",
    "app/main.py",
    "app/sync/backfill.py",
    "app/sync/webhook_processing.py",
)


@pytest.mark.parametrize("module", MODULES)
def test_scoped_functions_and_helpers_fit_the_ast_limits(module):
    for node in ast.walk(ast.parse((ROOT / module).read_text())):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        parameters = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
        count = sum(arg.arg not in {"self", "cls"} for arg in parameters)
        count += bool(node.args.vararg) + bool(node.args.kwarg)
        assert node.end_lineno - node.lineno + 1 <= 80, (module, node.name)
        assert count <= 6, (module, node.name)


def test_coverage_openapi_keeps_query_names_defaults_and_bounds():
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(coverage_router)
    operation = app.openapi()["paths"][PATH]["get"]
    assert "requestBody" not in operation
    parameters = operation["parameters"]
    assert [p["name"] for p in parameters] == [
        "start_date",
        "end_date",
        "timezone",
        "data_type",
        "offset",
        "limit",
    ]
    assert all(p["in"] == "query" for p in parameters)
    assert [p["required"] for p in parameters] == [True, True, True, False, False, False]
    assert [p["schema"] for p in parameters] == [
        {"type": "string", "format": "date", "title": "Start Date"},
        {"type": "string", "format": "date", "title": "End Date"},
        {"type": "string", "minLength": 1, "maxLength": 100, "title": "Timezone"},
        {"anyOf": [{"type": "string", "maxLength": 100}, {"type": "null"}], "title": "Data Type"},
        {"type": "integer", "minimum": 0, "maximum": 1000, "default": 0, "title": "Offset"},
        {"type": "integer", "minimum": 1, "maximum": 4, "default": 4, "title": "Limit"},
    ]


@pytest.mark.parametrize(
    "name,value",
    [
        ("start_date", "invalid"),
        ("end_date", "invalid"),
        ("timezone", ""),
        ("timezone", "x" * 101),
        ("data_type", "x" * 101),
        ("offset", -1),
        ("offset", 1001),
        ("limit", 0),
        ("limit", 5),
    ],
)
async def test_coverage_query_validation_keeps_http_error_locations(api, name, value):
    response = await api.get(PATH, params={**QUERY, name: value})
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["query", name]


@pytest.fixture(autouse=True)
def plain_cli_help(monkeypatch):
    # Typer forces terminal colours when GITHUB_ACTIONS is set; these contracts compare plain text.
    monkeypatch.setattr(typer.rich_utils, "FORCE_TERMINAL", False)


def test_identity_decide_keeps_cli_options_help_and_defaults():
    command = get_command(cli.app).commands["identity-decide"]
    assert [(p.name, p.opts, p.required, p.default) for p in command.params] == [
        ("decision", ["decision"], True, None),
        ("data_type", ["--data-type"], True, None),
        ("first", ["--from"], True, None),
        ("second", ["--to"], True, None),
        ("note", ["--note"], False, None),
        ("dry_run", ["--dry-run"], False, False),
        ("batch_size", ["--batch-size"], False, 5000),
    ]
    batch_size = command.params[-1].type
    assert (batch_size.min, batch_size.max) == (100, 100_000)
    result = CliRunner().invoke(cli.app, ["identity-decide", "--help"], color=False)
    assert result.exit_code == 0
    help_text = " ".join(result.stdout.replace("│", " ").split())
    for text in (
        "Decide what the reconciler left open",
        "--data-type",
        "--from",
        "--to",
        "--note",
        "Why (kept with the decision).",
        "--dry-run",
        "Report the effect; write nothing.",
        "--batch-size",
        "5000",
        "100<=x<=100000",
    ):
        assert text in help_text


@pytest.mark.parametrize("batch_size", [99, 100_001])
def test_identity_decide_rejects_out_of_range_cli_values(batch_size):
    result = CliRunner().invoke(
        cli.app,
        [
            "identity-decide",
            "link",
            "--data-type",
            "steps",
            "--from",
            "abcdef",
            "--to",
            "123456",
            "--batch-size",
            str(batch_size),
        ],
    )
    assert result.exit_code == 2
    assert "--batch-size" in result.output


@pytest.mark.parametrize("provider", [False, True])
async def test_grouped_pagination_keeps_reset_budget_and_request_callbacks(provider):
    client = AsyncMock()
    client.list_data_points.side_effect = [
        InvalidPageTokenError(),
        Page([{"id": "synthetic"}], "next"),
        Page([], None),
    ]
    events = []

    async def reset():
        events.append("reset")

    def requested():
        events.append("request")

    budget = PageBudget()
    budget.remaining = 2
    now = datetime(2026, 1, 1, tzinfo=UTC)
    if provider:
        pages = GoogleSource(client).fetch(
            REGISTRY["steps"],
            FetchWindow(now, now, "saved"),
            on_reset=reset,
            budget=budget,
            on_request=requested,
        )
    else:
        pages = iter_pages(
            client,
            REGISTRY["steps"],
            now,
            now,
            PaginationOptions("saved", budget, requested),
            on_reset=reset,
        )
    assert [p.data_points async for p in pages] == [[{"id": "synthetic"}], []]
    assert budget.remaining == 0
    assert events == ["request", "reset", "request", "request"]
    assert [call.args[-1] for call in client.list_data_points.await_args_list] == [
        "saved",
        None,
        "next",
    ]
