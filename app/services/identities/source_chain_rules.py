"""Reconciler chain decisions from complete sighting spans and device evidence.

The chain rule is documented in :mod:`app.services.identities.source_chains`.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

from .source_chain_evidence import (
    PAIR_KEYS,
    RETIRED_AFTER,
    ordered_pair,
    overlaps,
    precedes,
)
from .source_chain_model import Chains


@dataclass
class Decision:
    links: list[tuple[str, str]] = field(default_factory=list)
    # Per new link: the live links that kept another candidate out (``Chains.premises``).
    premises: dict[tuple[str, str], frozenset[tuple[str, str]]] = field(default_factory=dict)
    # One entry per fingerprint that might continue another but is not linked, with why.
    ambiguous: list[dict] = field(default_factory=list)


def describe(chains: Chains, fingerprint: str) -> dict:
    span = chains.spans.get(fingerprint)
    return {"source": span.label if span else "(unknown)", "fingerprint": fingerprint[:12]}


def decide_chains(chains: Chains) -> Decision:
    """Apply the rule in sighting order, recording every source left open and why."""
    rules = _ChainRules(chains)
    for members in kindred_groups(chains):
        if len(members) < 2:
            continue
        ordered = sorted(members, key=lambda name: (chains.spans[name].first, name))
        incomplete = [name for name in ordered if not chains.spans[name].complete]
        if incomplete:
            for name in incomplete:
                rules.note(name, "history incomplete: run identity-backfill (a full run) first")
            continue
        for new in ordered:
            if new not in chains.preceding:
                rules.consider(new, ordered)
    return rules.decision


class _ChainRules:
    """Candidate predicates over the current chains; only consider adds new links.

    Keep candidate checks in their original order: a new link changes membership
    and therefore the candidates and premises of every later source.
    """

    def __init__(self, chains: Chains) -> None:
        self.chains = chains
        self.decision = Decision()

    def note(self, new: str, why: str, candidates: Iterable[str] = ()) -> None:
        chains = self.chains
        spans = self.chains.spans
        self.decision.ambiguous.append(
            {
                **describe(chains, new),
                "first_seen": spans[new].first.isoformat(),
                "why": why,
                "candidates": [
                    describe(chains, name)
                    for name in sorted(
                        set(candidates), key=lambda name: (spans[name].first, spans[name].label)
                    )
                ],
            }
        )

    def apart(self, left: str, right: str) -> bool:
        chains = self.chains
        # Established distinct, or (only to stop blocking a chain, which merges equal readings
        # alone) ``PAIR_KEYS`` keys with two values in any responses, the owner not saying
        # otherwise: a second device whose evidence against one form comes from one re-fetch.
        # Two fingerprints that are not kindred are two devices.
        pair = ordered_pair(left, right)
        return (
            not chains.kindred(left, right)
            or chains.distinct(left, right)
            or (
                chains.pair_decisions.get(pair) != "not-distinct"
                and len({item.key for item in chains.evidence.get(pair, ())}) >= PAIR_KEYS
            )
        )

    def retired_before(self, name: str, other: str, new: str) -> bool:
        chains = self.chains
        spans = self.chains.spans
        # Another device than ``other`` (established distinct) whose newest record is more than
        # ``RETIRED_AFTER`` older than ``other``'s and than ``new``'s first sighting: it stopped
        # recording long before, so it is neither ``other``'s rival as a predecessor of ``new``
        # nor seen alongside ``new`` (its later sightings are re-fetches of its old records).
        # A device unworn for less than that (a watch on its charger) may be what ``new`` is.
        mine, theirs = spans[name].newest, spans[other].newest
        return (
            mine is not None
            and theirs is not None
            and mine + RETIRED_AFTER < theirs
            and mine + RETIRED_AFTER < spans[new].first
            and chains.distinct(name, other)
        )

    def blocks(self, other: str, old: str, new: str) -> bool:
        chains = self.chains
        # A source established distinct from one of them and apart from the other, one that
        # retired long before ``old`` did, or one that can be neither (another value of an
        # optional device field than both: another device) says nothing about old -> new.
        return not (
            not (chains.kindred(other, old) or chains.kindred(other, new))
            or (chains.distinct(other, old) and self.apart(other, new))
            or (chains.distinct(other, new) and self.apart(other, old))
            or self.retired_before(other, old, new)
        )

    def kin(self, name: str, old: str, new: str) -> bool:
        chains = self.chains
        # A form of either device is no other source: it says nothing about old -> new.
        return name in chains.members(old) or name in chains.members(new)

    def between(self, old: str, new: str, members: list[str], *, links: bool = True) -> list[str]:
        spans = self.chains.spans
        # ``links=False``: as if no live link made a form of either device (a premise).
        return [
            name
            for name in members
            if name not in (old, new)
            and not (links and self.kin(name, old, new))
            and self.blocks(name, old, new)
            and spans[old].last < spans[name].first < spans[new].first
        ]

    def premise_of(self, old: str, new: str, members: list[str]) -> frozenset[tuple[str, str]]:
        chains = self.chains
        spans = self.chains.spans
        # The live links without which another source of the class would have competed with
        # or blocked old -> new: a predecessor candidate that continues as another source, a
        # successor candidate that continues another one, a form of either device seen in
        # between or alongside.
        found: set[tuple[str, str]] = set()

        def device_links(name: str) -> set[tuple[str, str]]:
            device = chains.members(name)
            return {
                link
                for link in chains.links
                if link[0] in device and link[1] in device and chains.live(*link)
            }

        for name in members:
            if name in (old, new):
                continue
            if (
                name in chains.following
                and name not in chains.members(new)
                and chains.kindred(name, new)
                and chains.joinable(name, new)
                and not chains.distinct(name, new)
                and spans[name].last < spans[new].first
                and (
                    not precedes(spans[name], spans[new].first)
                    or not self.between(name, new, members, links=False)
                )
            ):
                found.add((name, chains.following[name]))
            if (
                name in chains.preceding
                and name not in chains.members(old)
                and chains.kindred(name, old)
                and chains.joinable(name, old)
                and not chains.distinct(name, old)
                and precedes(spans[old], spans[name].first)
                and not self.between(old, name, members, links=False)
            ):
                found.add((chains.preceding[name], name))
            if self.kin(name, old, new) and self.blocks(name, old, new):
                if spans[old].last < spans[name].first < spans[new].first or (
                    overlaps(spans[name], spans[old]) or overlaps(spans[name], spans[new])
                ):
                    found.update(device_links(name))
        found.discard((old, new))
        return frozenset(found)

    def earlier(self, new: str, ordered: list[str]) -> tuple[list[str], list[str]]:
        chains, spans = self.chains, self.chains.spans
        # A chain never forks; forms with conflicting optional device fields stay listed.
        earlier = [
            name
            for name in ordered
            if name not in chains.members(new)
            and name not in chains.following
            and chains.kindred(name, new)
            and not chains.distinct(name, new)
            and spans[name].first < spans[new].first
        ]
        unjoinable = [name for name in earlier if not chains.joinable(name, new)]
        earlier = [name for name in earlier if name not in unjoinable]
        ended_before = [name for name in earlier if spans[name].last < spans[new].first]
        earlier = [
            name
            for name in earlier
            if not any(
                self.retired_before(name, other, new) for other in ended_before if other != name
            )
        ]
        return earlier, unjoinable

    def predecessor(self, new: str, earlier: list[str], ordered: list[str]) -> str | None:
        spans = self.chains.spans
        ended = [name for name in earlier if spans[name].last < spans[new].first]
        if not ended:
            self.note(
                new,
                "seen alongside another source of its class (two devices, or a rename "
                "the sightings cannot tell apart)",
                earlier,
            )
            return None
        near = [name for name in ended if not precedes(spans[name], spans[new].first)]
        if near:
            self.note(new, "a source of its class ended less than five minutes before", near)
            return None
        predecessors = [old for old in ended if not self.between(old, new, ordered)]
        if not predecessors:
            self.note(new, "another source of its class appeared in between", ended)
            return None
        if len(predecessors) > 1:
            self.note(new, "more than one source of its class ended before it", predecessors)
            return None
        return predecessors[0]

    def successors(self, old: str, ordered: list[str]) -> list[str]:
        chains, spans = self.chains, self.chains.spans
        return [
            name
            for name in ordered
            if name not in chains.members(old)
            and name not in chains.preceding
            and chains.kindred(name, old)
            and chains.joinable(name, old)
            and not chains.distinct(name, old)
            and precedes(spans[old], spans[name].first)
            and not self.between(old, name, ordered)
        ]

    def alongside(self, old: str, new: str, ordered: list[str]) -> list[str]:
        spans = self.chains.spans
        return [
            name
            for name in ordered
            if not self.kin(name, old, new)
            and self.blocks(name, old, new)
            and (overlaps(spans[name], spans[old]) or overlaps(spans[name], spans[new]))
        ]

    def consider(self, new: str, ordered: list[str]) -> None:
        chains = self.chains
        earlier, unjoinable = self.earlier(new, ordered)
        if not earlier:
            if unjoinable:
                self.note(
                    new,
                    "its earlier form belongs to a device with another value of a device "
                    "field (form factor or manufacturer): never linked",
                    unjoinable,
                )
            return
        old = self.predecessor(new, earlier, ordered)
        if old is None:
            return
        if (old, new) in chains.links or (new, old) in chains.links:
            self.note(new, "a link between them was rejected or refuted", [old])
            return
        if old in chains.following:
            self.note(new, "its predecessor already continues as another source", [old])
            return
        successors = self.successors(old, ordered)
        if successors != [new]:
            self.note(new, "its predecessor has more than one successor candidate", successors)
            return
        alongside = self.alongside(old, new, ordered)
        if alongside:
            self.note(new, "another source of its class was seen alongside", alongside)
            return
        if chains.reaches(new, old):
            self.note(new, "linking would close a cycle", [old])
            return
        premise = self.premise_of(old, new, ordered)
        chains.add(old, new, premise=premise)
        if premise:
            self.decision.premises[(old, new)] = premise
        self.decision.links.append((old, new))


def kindred_groups(chains: Chains) -> list[list[str]]:
    """The fingerprints connected by kindred pairs, per source family (the rule runs per group:
    without optional device fields a group is exactly a stable class)."""
    families: dict[str, list[str]] = defaultdict(list)
    for fingerprint, span in chains.spans.items():
        families[span.family].append(fingerprint)
    groups: list[list[str]] = []
    for members in families.values():
        left = set(members)
        while left:
            start = min(left)
            left.discard(start)
            group, frontier = [start], [start]
            while frontier:
                current = frontier.pop()
                joined = sorted(name for name in left if chains.kindred(current, name))
                for name in joined:
                    left.discard(name)
                group.extend(joined)
                frontier.extend(joined)
            groups.append(sorted(group))
    return groups
