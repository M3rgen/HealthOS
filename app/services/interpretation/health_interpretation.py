"""Production entry point for bounded interpretation actions, including bound v2 cursors."""

import base64
import json
from collections.abc import Collection, Sequence
from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.parsers.common import payload_hash
from app.services.shared.query_ranges import CivilDateRange, SeriesFilter

from .archive_query import ArchiveQuery
from .derived import BUILD_FAILED, derived_range
from .health_windows import interpretation_window
from .interpretation_inputs import BudgetError, Inputs, load_inputs
from .interpretation_intraday import intraday_summaries, sleep_summaries
from .interpretation_measures import (
    PHYSIOLOGY,
    QUANTITIES,
    measurement_summaries,
    session_summaries,
)
from .interpretation_stats import (
    DAILY_KINDS,
    baselines,
    candidates,
    daily_series,
    relationships,
)
from .interpretation_types import Interpretation, Refusal, Summary

RESPONSE_BUDGET_BYTES = 64 * 1024
MAX_SUMMARIES = 1000
MAX_RELATIONSHIP_REGIMES = 16
# Fixed whole-query allocation ceilings, independent of page size and cursor.
# References count both pair sides and every reuse across summaries/lags.
MAX_RELATIONSHIP_PAIRS = 20_000
MAX_RELATIONSHIP_REFERENCES = 40_000
DERIVED_ACTIONS = frozenset({"baseline", "analyze", "relationship", "sleep"})


# Whole-query work budgets: the same ceiling holds for every page size and cursor.
SCOPE_BUDGETS = ("baseline_", "intraday_", "overlap_", "relationship_", "summary_")
SCOPE_ERROR = "Yorum sorgusu tamamlanamadı; aralığı ya da metriği daralt."


def refusal(code: str) -> dict[str, Any]:
    result = Refusal(code=code, reason_codes=[code])
    if code.startswith(SCOPE_BUDGETS) and code.endswith("_budget_exceeded"):
        result.error = SCOPE_ERROR
    return result.model_dump(mode="json")


def encode_cursor(query_hash: str, snapshot: str, offset: int) -> str:
    return base64.urlsafe_b64encode(
        json.dumps(["interpretation/2", query_hash, snapshot, offset]).encode()
    ).decode()


def cursor_offset(cursor: str | None, query_hash: str, snapshot: str) -> int:
    if cursor is None:
        return 0
    try:
        version, bound, state, offset = json.loads(
            base64.b64decode(cursor, altchars=b"-_", validate=True)
        )
        if version != "interpretation/2" or bound != query_hash:
            raise BudgetError("cursor_query_mismatch")
        if state != snapshot:
            raise BudgetError("stale_cursor")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError
        return offset
    except (ValueError, TypeError, UnicodeDecodeError):
        raise BudgetError("invalid_cursor") from None


def derived_window(query: ArchiveQuery):
    """The derived days an action reads, so they can be built before its snapshot."""
    if query.action not in DERIVED_ACTIONS or query.data_type:
        return None  # a data_type is refused by these actions before any read
    first, last = query.date_range()
    return interpretation_window(query.action, first, last, query.lookback_days)


async def interpret(session: AsyncSession, query: ArchiveQuery) -> dict[str, Any]:
    first, last = query.date_range()
    if query.data_type:
        if query.data_type not in _accepted_data_types(query):
            return refusal("unsupported_data_type")
    read_first, read_last = interpretation_window(query.action, first, last, query.lookback_days)
    kinds = _input_kinds(query)

    async def inputs_now():
        return await load_inputs(
            session,
            kinds,
            manual=query.action == "longitudinal",
            workouts=query.action == "sessions",
            dates=CivilDateRange(read_first, read_last, query.timezone),
        )

    try:
        inputs = await inputs_now()
        derived = None
        if query.action in DERIVED_ACTIONS:
            # Days are built beforehand; in a PostgreSQL REPEATABLE READ snapshot an
            # unbuilt day is only reported, never built from that snapshot.
            derived = await derived_range(session, read_first, read_last, query.timezone)
            if any(day["state"] == "error" for day in derived["days"]):
                return refusal(BUILD_FAILED)
            if any(day["state"] != "current" for day in derived["days"]):
                return refusal("inputs_changed_retry")
        summaries, recommendations = _summaries(query, derived, inputs, first, last)
        if query.action not in {"baseline", "analyze", "relationship"}:
            summaries = [
                row
                for row in summaries
                if (not query.source or row.source == query.source)
                and (not query.definition or row.definition == query.definition)
            ]
        if len(summaries) > MAX_SUMMARIES:
            return refusal("summary_budget_exceeded")
        if (await inputs_now()).fingerprint != inputs.fingerprint:
            return refusal("stale_cursor" if query.cursor else "inputs_changed_retry")
        # Limit may vary between pages. All scientific inputs and action parameters are bound.
        query_hash = payload_hash(query.model_dump(mode="json", exclude={"cursor", "limit"}))
        snapshot = payload_hash(
            [inputs.fingerprint, [s.model_dump(mode="json") for s in summaries]]
        )
        offset = cursor_offset(query.cursor, query_hash, snapshot)
        if offset > len(summaries):
            return refusal("invalid_cursor")
        stop = min(offset + query.limit, len(summaries))
        # Candidate references use absolute indices into the full, stable summary sequence.
        result = Interpretation(
            action=query.action,
            timezone=query.timezone,
            start_date=str(first),
            end_date=str(last),
            summaries=list(summaries[offset:stop]),
            candidates=recommendations,
            total_summaries=len(summaries),
            summary_offset=offset,
            next_cursor=encode_cursor(query_hash, snapshot, stop)
            if stop < len(summaries)
            else None,
            reason_codes=[] if summaries else ["no_eligible_evidence_in_window"],
        )
        encoded = result.model_dump(mode="json")
        if (
            len(json.dumps(encoded, ensure_ascii=False, allow_nan=False).encode())
            > RESPONSE_BUDGET_BYTES
        ):
            return refusal("response_budget_exceeded")
        return encoded
    except BudgetError as exc:
        return refusal(str(exc))


def _summaries(
    query: ArchiveQuery, derived: dict[str, Any] | None, inputs: Inputs, first: date, last: date
):
    recommendations = []
    summaries: Sequence[Summary]
    if query.action in {"baseline", "analyze", "relationship"}:
        if derived is None:
            raise RuntimeError("Derived interpretation window is missing")
        series = daily_series(derived, inputs)
        if query.action == "relationship":
            if len(series) > MAX_RELATIONSHIP_REGIMES:
                raise BudgetError("relationship_regime_budget_exceeded")
            summaries = relationships(
                series,
                query,
                max_summaries=MAX_SUMMARIES,
                max_pairs=MAX_RELATIONSHIP_PAIRS,
                max_references=MAX_RELATIONSHIP_REFERENCES,
            )
        else:
            summaries = baselines(
                series,
                first,
                last,
                query.lookback_days,
                max_summaries=MAX_SUMMARIES,
                selection=SeriesFilter(query.metric, query.source, query.definition),
            )
            if query.action == "analyze":
                recommendations = candidates(summaries)
    elif query.action == "sleep":
        if derived is None:
            raise RuntimeError("Derived interpretation window is missing")
        summaries = sleep_summaries(derived, first, last)
    elif query.action == "intraday":
        summaries = intraday_summaries(inputs, query, max_summaries=MAX_SUMMARIES)
    elif query.action == "sessions":
        summaries = session_summaries(inputs, query, max_summaries=MAX_SUMMARIES)
    else:
        summaries = measurement_summaries(inputs, query, max_summaries=MAX_SUMMARIES)
    return summaries, recommendations


def _accepted_data_types(query: ArchiveQuery) -> Collection[str]:
    return (
        PHYSIOLOGY
        if query.action == "physiology"
        else {*QUANTITIES, "muscle"}
        if query.action == "longitudinal"
        else {"steps", "heart-rate"}
        if query.action == "intraday"
        else set()
    )


def _input_kinds(query: ArchiveQuery) -> set[str]:
    return (
        set(DAILY_KINDS.values())
        if query.action in {"baseline", "analyze", "sleep", "relationship"}
        else {query.data_type}
        if query.data_type
        else {"steps", "heart-rate"}
        if query.action == "intraday"
        else {"exercise"}
        if query.action == "sessions"
        else set(PHYSIOLOGY)
        if query.action == "physiology"
        else set(QUANTITIES)
    )
