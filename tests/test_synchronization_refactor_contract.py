"""Keep synchronization refactors bounded and scheduler callers compatible."""

import ast
from dataclasses import FrozenInstanceError, asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.config import Settings
from app.services.synchronization.scheduler import SchedulerServices, build_scheduler

ROOT = Path(__file__).resolve().parents[1] / "app/services/synchronization"


@pytest.mark.parametrize(
    "module", ["coverage", "pipeline_health", "retrieval_coverage", "scheduler", "webhook_status"]
)
def test_synchronization_functions_and_helpers_fit_the_issue_budgets(module):
    for node in ast.walk(ast.parse((ROOT / f"{module}.py").read_text())):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        assert node.end_lineno - node.lineno + 1 <= 80, node.name
        args = node.args
        parameters = [*args.posonlyargs, *args.args, *args.kwonlyargs]
        count = sum(arg.arg not in {"self", "cls"} for arg in parameters)
        count += int(args.vararg is not None) + int(args.kwarg is not None)
        assert count <= 6, node.name


def scheduler_workers():
    return SchedulerServices(
        watchdog=SimpleNamespace(periodic_check=AsyncMock()),
        pipeline_health=SimpleNamespace(refresh_freshness=AsyncMock()),
        identity=SimpleNamespace(run=AsyncMock()),
        derived=SimpleNamespace(run=AsyncMock()),
        garmin=SimpleNamespace(run=AsyncMock(), enabled=True),
        webhooks=SimpleNamespace(process_unfinished=AsyncMock()),
    )


def scheduled_jobs(scheduler):
    return {
        job.id: (
            job.func,
            str(job.trigger),
            job.args,
            job.kwargs,
            job.max_instances,
            job.coalesce,
            job.misfire_grace_time,
        )
        for job in scheduler.get_jobs()
    }


@pytest.mark.parametrize("selection", [("none",), ("google",), ("garmin",), ("google", "garmin")])
@pytest.mark.parametrize("reconcile", [False, True])
def test_grouped_workers_preserve_jobs_and_legacy_calls(selection, reconcile):
    settings = Settings(
        _env_file=None,
        healthos_sources=selection,
        reconciliation_enabled=reconcile,
        identity_reconcile_enabled=True,
        watchdog_enabled=True,
    )
    reconciliation = SimpleNamespace(run=AsyncMock())
    workers = scheduler_workers()
    # Keep the workers themselves: asdict would deepcopy mocks and lose callback identity.
    named = vars(workers)
    grouped = build_scheduler(settings, reconciliation, services=workers)
    positional = build_scheduler(settings, reconciliation, *named.values())
    keywords = build_scheduler(settings, reconciliation, **named)
    mixed = build_scheduler(
        settings, reconciliation, workers.watchdog, **dict(list(named.items())[1:])
    )
    expected = {"derived-refresh"}
    if set(selection) & {"google", "garmin"}:
        expected.add("pipeline-watchdog")
    if "google" in selection:
        expected.update({"pipeline-freshness-refresh", "webhook-unfinished"})
        expected.update(
            {"daily-reconciliation", "weekly-reconciliation"}
            if reconcile
            else {"identity-reconcile"}
        )
    if "garmin" in selection:
        expected.add("garmin-pull")
    assert set(scheduled_jobs(grouped)) == expected
    for scheduler in (positional, keywords, mixed):
        assert scheduled_jobs(scheduler) == scheduled_jobs(grouped)
        # Chaining creates a distinct closure, with the same event subscriptions.
        assert [(callback.__name__, mask) for callback, mask in scheduler._listeners] == [
            (callback.__name__, mask) for callback, mask in grouped._listeners
        ]
    for job in grouped.get_jobs():
        assert (job.max_instances, job.coalesce, job.misfire_grace_time) == (1, True, 3600)


def test_scheduler_worker_defaults_and_immutable_group():
    settings = Settings(_env_file=None)
    reconciliation = SimpleNamespace(run=AsyncMock())
    workers = SchedulerServices()
    assert set(asdict(workers).values()) == {None}
    with pytest.raises(FrozenInstanceError):
        workers.webhooks = SimpleNamespace(process_unfinished=AsyncMock())
    assert scheduled_jobs(build_scheduler(settings, reconciliation)) == scheduled_jobs(
        build_scheduler(settings, reconciliation, services=workers)
    )


@pytest.mark.parametrize(
    ("positional", "keywords"),
    [
        ((None,) * 7, {}),
        ((), {"unknown_worker": None}),
        ((None,), {"watchdog": None}),
        ((None,), {"services": SchedulerServices()}),
        ((), {"services": SchedulerServices(), "derived": None}),
    ],
)
def test_scheduler_rejects_invalid_worker_arguments(positional, keywords):
    with pytest.raises(TypeError):
        build_scheduler(Settings(_env_file=None), SimpleNamespace(), *positional, **keywords)
