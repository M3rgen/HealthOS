"""Request context and received-body state for the Google transport."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class RequestContext:
    method: str
    path: str
    access_token: str | None
    data_type: str | None
    kwargs: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ReceivedBody:
    wire: bytes
    cut_off: BaseException | None
    failure: BaseException | None
