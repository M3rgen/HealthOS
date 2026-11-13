from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime

from app.core.fetch_window import FetchBudget
from app.core.fetch_window import PaginationPaused as PaginationPaused
from app.google_health.client import GoogleHealthClient, InvalidPageTokenError, Page
from app.google_health.data_types import DataTypeSpec

# A turn must release ownership even if Google always returns a new continuation.
MAX_PAGES_PER_CALL = 1000


class RepeatedPageTokenError(RuntimeError):
    """A cyclic response, distinct from an expired request token.

    Raised after the consumer stored the cyclic page's records. The last accepted
    checkpoint stays resumable. Neither a reset nor a retry of the first page can
    turn the repeated continuation into valid progress. Keep the opaque token and
    response contents out of the exception and its chain.
    """

    def __init__(self) -> None:
        super().__init__("Google Health API returned a repeated pagination token")


class PageBudget:
    """Shared by all windows one backfill or sync invocation fetches for a data type."""

    def __init__(self):
        self.remaining = MAX_PAGES_PER_CALL


@dataclass(frozen=True)
class PaginationOptions:
    """A continuation and the shared accounting for the fetch that resumes it."""

    page_token: str | None = None
    budget: FetchBudget | None = None
    on_request: Callable[[], None] | None = None


async def iter_pages(
    client: GoogleHealthClient,
    spec: DataTypeSpec,
    start: datetime,
    end: datetime,
    page_token: str | PaginationOptions | None = None,
    *,
    on_reset: Callable[[], Awaitable[None]] | None = None,
) -> AsyncIterator[Page]:
    """Yield every page; a page whose continuation repeats ends the attempt.

    The request token also guards a resumed checkpoint's first response. History
    is local to this invocation and is cleared only for the one permitted reset;
    continuations are compared verbatim, never normalized. A cyclic page is still
    yielded, marked ``cyclic``: its records may be new, so the consumer stores them
    and their responses in its page transaction, but never commits the repeated
    token as a checkpoint. The next request raises RepeatedPageTokenError instead.
    """
    # A bare token remains shorthand for callers that need no shared accounting.
    options = (
        page_token if isinstance(page_token, PaginationOptions) else PaginationOptions(page_token)
    )
    page_token = options.page_token
    budget = options.budget if options.budget is not None else PageBudget()
    seen_tokens: set[str] = {page_token} if page_token else set()
    token = page_token
    token_reset = False
    while True:
        if budget.remaining <= 0:
            raise PaginationPaused("Google Health page budget exhausted")
        if options.on_request is not None:
            options.on_request()
        try:
            page = await client.list_data_points(spec, start, end, token)
        except InvalidPageTokenError:
            if token_reset:
                raise
            if on_reset is not None:
                await on_reset()
            # Let the consumer commit its reset before clearing the old history.
            # The new attempt may legitimately return tokens from the old one.
            token = None
            seen_tokens.clear()
            token_reset = True
            continue
        next_token = page.next_page_token
        budget.remaining -= 1
        if next_token and (next_token == token or next_token in seen_tokens):
            # Raised even at the budget boundary: a cycle is a failure, not a pause.
            yield replace(page, cyclic=True)
            raise RepeatedPageTokenError()
        yield page
        if not next_token:
            return
        seen_tokens.add(next_token)
        token = next_token
