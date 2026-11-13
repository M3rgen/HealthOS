"""A requested interval and its opaque continuation, kept together when resumed."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class FetchWindow:
    start: datetime
    end: datetime
    page_token: str | None = None


class FetchBudget(Protocol):
    remaining: int


class PaginationPaused(Exception):
    """Committed pages can be resumed in a later invocation after its budget is renewed."""
