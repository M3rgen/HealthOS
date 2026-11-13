"""Function budgets and grouped context contracts for issue #480."""

import ast
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.core.fetch_window import FetchWindow
from app.core.ingestion_stats import StoreStats
from app.services.providers import garmin_normalize
from app.services.providers.ingestion_contracts import FetchControls, ingest_pages
from app.services.umay import health_transfer

ROOT = Path(__file__).resolve().parents[1] / "app/services"
MODULES = (
    "archive/raw_history",
    "interpretation/health_interpretation",
    "interpretation/interpretation_stats",
    "providers/garmin_normalize",
    "providers/garmin_normalize_writing",
    "providers/ingestion_contracts",
    "umay/chat_relay",
    "umay/health_transfer",
)


@pytest.mark.parametrize("module", MODULES)
def test_refactored_functions_and_helpers_fit_the_issue_budgets(module):
    for node in ast.walk(ast.parse((ROOT / f"{module}.py").read_text())):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        assert node.end_lineno - node.lineno + 1 <= 80, node.name
        args = node.args
        parameters = [*args.posonlyargs, *args.args, *args.kwonlyargs]
        count = sum(arg.arg not in {"self", "cls"} for arg in parameters)
        count += int(args.vararg is not None) + int(args.kwarg is not None)
        assert count <= 6, node.name


@pytest.mark.parametrize(
    ("context", "field"),
    [
        (FetchControls(), "budget"),
        (garmin_normalize._ScalarWindow(datetime(2026, 1, 1), datetime(2026, 1, 2)), "start"),
        (health_transfer._SummaryDates([], [], "UTC"), "timezone"),
    ],
)
def test_grouped_context_fields_are_frozen(context, field):
    with pytest.raises(FrozenInstanceError):
        setattr(context, field, None)


@pytest.mark.parametrize("configured", [False, True])
async def test_ingestion_controls_preserve_callbacks_budget_and_committed_progress(configured):
    stream = SimpleNamespace(name="synthetic")
    start = datetime(2026, 1, 1, tzinfo=UTC)
    window = FetchWindow(start, start + timedelta(days=1))
    pages = [SimpleNamespace(data_points=[number]) for number in (1, 2)]
    budget = SimpleNamespace(remaining=2)
    reset, request = AsyncMock(), Mock()
    controls = FetchControls(on_reset=reset, budget=budget, on_request=request)
    commit = AsyncMock(side_effect=[StoreStats(inserted=1), StoreStats(updated=1)])

    class Source:
        keep_after = AsyncMock()

        async def fetch(self, actual_stream, actual_window, *, on_reset, budget, on_request):
            assert actual_stream is stream and actual_window is window
            assert (on_reset, budget, on_request) == (
                (controls.on_reset, controls.budget, controls.on_request)
                if configured
                else (None, None, None)
            )
            if on_reset is not None:
                await on_reset()
            for page in pages:
                # Progress is observed only after each page's commit finishes.
                assert commit.await_count == len(observed)
                if on_request is not None:
                    on_request()
                if budget is not None:
                    budget.remaining -= 1
                yield page

    source = Source()
    observed = []
    kwargs = {"controls": controls} if configured else {}
    async for page, stats in ingest_pages(source, stream, window, commit, **kwargs):
        assert commit.await_count == len(observed) + 1
        observed.append((page, stats.inserted, stats.updated))
    assert observed == [(pages[0], 1, 0), (pages[1], 0, 1)]
    assert [call.args for call in commit.await_args_list] == [(page,) for page in pages]
    assert reset.await_count == int(configured)
    assert request.call_count == (2 if configured else 0)
    assert budget.remaining == (0 if configured else 2)
    source.keep_after.assert_not_awaited()
