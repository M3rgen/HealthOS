"""Owned source-chain state and cache-invalidating updates.

Readers get read-only collections. Loading recorded evidence deliberately bypasses the
usefulness filter applied to newly discovered evidence.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import TypedDict, Unpack

from app.core.timeutil import as_utc
from app.models.identity_constants import CHAIN_DECISIONS
from app.services.identities.source_chain_evidence import Evidence, Span, ordered_pair, useful


class _HistoryFields(TypedDict, total=False):
    chain_decisions: Mapping[tuple[str, str], str] | None
    pair_decisions: Mapping[tuple[str, str], str] | None
    refuted_events: Iterable[tuple[str, str]]
    logged_devices: Mapping[str, str] | None
    agreement: Mapping[tuple[str, str], tuple[int, int, bool]] | None
    premises: Mapping[tuple[str, str], frozenset[tuple[str, str]]] | None


@dataclass(frozen=True)
class ChainHistory:
    """Recorded decisions, refutations and reader state supplied when copying chains."""

    chain_decisions: Mapping[tuple[str, str], str] | None = None
    pair_decisions: Mapping[tuple[str, str], str] | None = None
    refuted_events: Iterable[tuple[str, str]] = ()
    logged_devices: Mapping[str, str] | None = None
    agreement: Mapping[tuple[str, str], tuple[int, int, bool]] | None = None
    premises: Mapping[tuple[str, str], frozenset[tuple[str, str]]] | None = None


class ChainState:
    """Storage and updates shared by the source-chain queries in ``Chains``."""

    def __init__(
        self,
        links: Iterable[tuple[str, str]] = (),
        spans: Mapping[str, Span] | None = None,
        decided_by: Mapping[tuple[str, str], str] | None = None,
        evidence: Mapping[tuple[str, str], Iterable[Evidence]] | None = None,
        *,
        history: ChainHistory | None = None,
        **recorded: Unpack[_HistoryFields],
    ) -> None:
        # Preserve existing keyword callers while new code supplies the grouped history.
        if history is not None and recorded:
            raise TypeError("use either history or individual history fields")
        history = history if history is not None else ChainHistory(**recorded)
        self._links = frozenset(links)
        self._spans = dict(spans or {})
        self._decided_by = dict(decided_by or {})
        self._evidence = {pair: tuple(items) for pair, items in (evidence or {}).items()}
        self._chain_decisions = dict(history.chain_decisions or {})
        self._pair_decisions = dict(history.pair_decisions or {})
        self._refuted_events = frozenset(history.refuted_events)
        self._logged_devices = dict(history.logged_devices or {})
        self._agreement = dict(history.agreement or {})
        self._premises = dict(history.premises or {})
        self._changed()

    @property
    def links(self) -> frozenset[tuple[str, str]]:
        """Every recorded link (from, to), live or not."""
        return self._links

    @property
    def spans(self) -> Mapping[str, Span]:
        """Sighting spans by fingerprint."""
        return MappingProxyType(self._spans)

    @property
    def decided_by(self) -> Mapping[tuple[str, str], str]:
        """Who decided each recorded link."""
        return MappingProxyType(self._decided_by)

    @property
    def evidence(self) -> Mapping[tuple[str, str], tuple[Evidence, ...]]:
        """Ordered pair -> keys returned under both with two readings."""
        return MappingProxyType(self._evidence)

    @property
    def chain_decisions(self) -> Mapping[tuple[str, str], str]:
        """The owner's latest link/reject/confirm decision per link."""
        return MappingProxyType(self._chain_decisions)

    @property
    def pair_decisions(self) -> Mapping[tuple[str, str], str]:
        """The owner's latest distinct/not-distinct decision per ordered pair."""
        return MappingProxyType(self._pair_decisions)

    @property
    def refuted_events(self) -> frozenset[tuple[str, str]]:
        """Links with a refuted event."""
        return self._refuted_events

    @property
    def logged_devices(self) -> Mapping[str, str]:
        """The latest logged device of each fingerprint (what readers use)."""
        return MappingProxyType(self._logged_devices)

    @property
    def agreement(self) -> Mapping[tuple[str, str], tuple[int, int, bool]]:
        """Ordered pair -> (same-reading keys, two-reading keys, all keys counted)."""
        return MappingProxyType(self._agreement)

    @property
    def premises(self) -> Mapping[tuple[str, str], frozenset[tuple[str, str]]]:
        """The live links a reconciler link relied on."""
        return MappingProxyType(self._premises)

    def _changed(self) -> None:
        self._following: dict[str, str] | None = None
        self._preceding: dict[str, str] | None = None
        self._devices: dict[str, frozenset[str]] | None = None
        self._joined: dict[str, frozenset[str]] | None = None
        self._distinct: dict[tuple[str, str], bool] = {}
        self._premise_live: dict[tuple[str, str], bool] = {}

    def add(
        self,
        old: str,
        new: str,
        *,
        decided_by: str | None = None,
        premise: frozenset[tuple[str, str]] = frozenset(),
    ) -> None:
        """A link planned in memory (live unless decided otherwise)."""
        self._links |= {(old, new)}
        if decided_by is not None:
            self._decided_by[(old, new)] = decided_by
        if premise:
            self._premises[(old, new)] = premise
        self._changed()

    def forget(self, old: str, new: str) -> None:
        """Drop a link planned in memory that could not be recorded."""
        self._links -= {(old, new)}
        self._changed()

    def decide(self, decision: str, first: str, second: str) -> None:
        """Apply an owner decision in memory (a dry run, or one just recorded)."""
        if decision in CHAIN_DECISIONS:
            self._chain_decisions[(first, second)] = decision
        else:
            self._pair_decisions[ordered_pair(first, second)] = decision
        self._changed()

    def add_evidence(self, left: str, right: str, key: str, seen: datetime) -> bool:
        found = self._evidence.get(ordered_pair(left, right), ())
        item = Evidence(key, as_utc(seen))
        if not useful(list(found), item):
            return False
        self._evidence[ordered_pair(left, right)] = (*found, item)
        self._changed()
        return True

    def load_evidence(self, left: str, right: str, item: Evidence) -> None:
        """Load recorded evidence as-is, including keys no longer useful to a new sample."""
        pair = (left, right)
        self._evidence[pair] = (*self._evidence.get(pair, ()), item)
        self._changed()

    def refute(self, links: Iterable[tuple[str, str]]) -> None:
        """Apply recorded or dry-run refutations."""
        self._refuted_events |= frozenset(links)
        self._changed()

    def log_devices(self, devices: Mapping[str, str]) -> None:
        """Apply the latest logged devices."""
        self._logged_devices.update(devices)
        self._changed()

    def update_agreement(self, agreement: Mapping[tuple[str, str], tuple[int, int, bool]]) -> None:
        """Apply the latest counted agreement for the given pairs."""
        self._agreement.update(agreement)
        self._changed()

    def set_premise(self, old: str, new: str, premise: frozenset[tuple[str, str]]) -> None:
        """Load the recorded live links this link relied on."""
        self._premises[(old, new)] = premise
        self._changed()

    def set_span(self, fingerprint: str, span: Span) -> None:
        """Load or replace a fingerprint's sighting span."""
        self._spans[fingerprint] = span
        self._changed()

    def complete_spans(self) -> None:
        """A full backfill established the history of every loaded fingerprint."""
        for span in self._spans.values():
            span.complete = True
        self._changed()
