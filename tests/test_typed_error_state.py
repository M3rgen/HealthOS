"""Error decisions use typed state; production signatures stay within the issue budget."""

import ast
import inspect
from pathlib import Path
from typing import get_type_hints

import httpx
import pytest

from app.google_health.client import InvalidPageTokenError, InvalidResponseError, Page
from app.google_health.errors import GoogleHealthError, GoogleHealthErrorCode, RetryDeferredError
from app.google_health.oauth import OAuthError
from app.google_health.pagination import RepeatedPageTokenError
from app.services.archive.storage import _lock_rows, store_page
from app.services.identities.identity_common import Resolution
from app.sync import errors

ROOT = Path(__file__).resolve().parents[1]


class UnprintableGoogleError(GoogleHealthError):
    def __str__(self):
        raise AssertionError("classification must never read diagnostic text")


@pytest.mark.parametrize("status", [206, 302, 400, 401, 403, 429, 500, 503, 599, 600])
@pytest.mark.parametrize("error_type", [UnprintableGoogleError, RetryDeferredError])
def test_status_fields_determine_summary_and_transience(status, error_type):
    failure = error_type("HTTP 503 connection failed", http_status=status)
    assert failure.http_status == status
    assert errors.error_code(failure) == f"http_{status}"
    assert errors.error_summary(failure) == f"{error_type.__name__} (http_{status})"
    assert errors.is_transient(failure) is (status == 429 or 500 <= status < 600)


@pytest.mark.parametrize("code", list(GoogleHealthErrorCode))
def test_explicit_codes_take_precedence_over_status_and_message(code):
    failure = UnprintableGoogleError("irrelevant", code=code, http_status=503)
    assert failure.code is code
    assert errors.error_code(failure) == code.value
    assert errors.error_summary(failure) == f"UnprintableGoogleError ({code.value})"
    assert errors.is_transient(failure) is (code is GoogleHealthErrorCode.CONNECTION)


@pytest.mark.parametrize("error_type", [RuntimeError, GoogleHealthError])
@pytest.mark.parametrize(
    "message",
    ["HTTP 503", "HTTP 429", "connection failed", "invalid JSON", "repeated pagination token"],
)
def test_message_alone_cannot_supply_error_state(error_type, message):
    failure = error_type(message)
    assert errors.error_code(failure) is None
    assert errors.error_summary(failure) == error_type.__name__
    assert errors.is_transient(failure) is False


@pytest.mark.parametrize(
    "failure, code",
    [
        (InvalidResponseError("HTTP 503", http_status=503), "invalid_response"),
        (InvalidPageTokenError("HTTP 503", http_status=503), "invalid_page_token"),
        (RepeatedPageTokenError(), "repeated_page_token"),
    ],
)
def test_deterministic_errors_keep_their_classification(failure, code):
    assert errors.error_code(failure) == code
    assert not errors.is_transient(failure)


def test_transport_retries_do_not_need_a_message():
    assert errors.is_transient(httpx.ReadError("arbitrary wording"))


def test_error_classifier_has_no_text_parser():
    tree = ast.parse(inspect.getsource(errors))
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"str", "repr"}
        for node in ast.walk(tree)
    )
    assert not any(
        isinstance(node, ast.Import)
        and any(alias.name == "re" for alias in node.names)
        or isinstance(node, ast.ImportFrom)
        and node.module == "re"
        for node in ast.walk(tree)
    )


def test_at_most_fifteen_production_functions_have_seven_explicit_parameters():
    # Include self and keyword-only parameters; no file or function exemptions.
    large = []
    for path in (ROOT / "app").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                count = len(node.args.posonlyargs + node.args.args + node.args.kwonlyargs)
                if count >= 7:
                    large.append(f"{path.relative_to(ROOT)}:{node.name} ({count})")
    assert len(large) <= 15, large


def test_storage_page_and_resolution_have_concrete_types():
    assert get_type_hints(store_page)["page"] is Page
    assert get_type_hints(_lock_rows)["resolution"] is Resolution


def test_oauth_state_keeps_its_summary_and_explicit_retry_policy():
    failure = OAuthError("wording changed", http_status=503, transient=False)
    assert errors.error_summary(failure) == "OAuthError (http_503)"
    assert not errors.is_transient(failure)
    failure = OAuthError("wording changed", code=GoogleHealthErrorCode.CONNECTION, transient=True)
    assert errors.error_summary(failure) == "OAuthError (connection)"
    assert errors.is_transient(failure)


@pytest.mark.parametrize(
    "error_type",
    [GoogleHealthError, InvalidResponseError, InvalidPageTokenError, RetryDeferredError],
)
def test_google_errors_can_still_be_constructed_without_a_message(error_type):
    failure = error_type()
    assert str(failure) == ""
    assert not errors.is_transient(failure)


async def test_incremental_state_keeps_the_requested_registry_key(sessions):
    from datetime import UTC, datetime, timedelta
    from unittest.mock import AsyncMock

    from app.core.config import Settings
    from app.google_health.data_types import REGISTRY
    from app.models.state import JobStatus, SyncState
    from app.services.providers.ingestion import GoogleSource
    from app.sync.incremental import IncrementalSync

    client = AsyncMock()
    client.list_data_points.return_value = Page([], None)
    sync = IncrementalSync(
        Settings(_env_file=None), sessions, GoogleSource(client), {"selected": REGISTRY["steps"]}
    )
    start = datetime(2026, 1, 1, tzinfo=UTC)
    await sync.run("selected", start, start + timedelta(days=1))
    async with sessions() as session:
        state = await session.get(SyncState, "selected")
        assert state.status == JobStatus.completed and state.last_error is None
        assert await session.get(SyncState, "steps") is None
