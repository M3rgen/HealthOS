"""Civil date and source selections shared by bounded archive queries."""

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True)
class CivilDateRange:
    first: date
    last: date
    timezone: str


@dataclass(frozen=True, slots=True)
class SeriesFilter:
    metric: str | None = None
    source: str | None = None
    definition: str | None = None


DEFAULT_SERIES_FILTER = SeriesFilter()
