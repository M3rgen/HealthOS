"""Characterize decision ordering and complete diagnostics of the public rule entry points."""

from datetime import timedelta

import pytest
from identity_records import T0

from app.models.identity_tables import RawSourceChain
from app.services.identities.identity_verification import verify_identities
from app.services.identities.source_chain_evidence import Span
from app.services.identities.source_chain_model import Chains
from app.services.identities.source_chain_rules import Decision, decide_chains


def span(first, last, label, complete=True):
    return Span(T0 + timedelta(hours=first), T0 + timedelta(hours=last), "band", label, complete)


def test_planned_links_affect_later_candidates_and_repeated_calls():
    chains = Chains(spans={"c": span(6, 7, "C"), "b": span(3, 4, "B"), "a": span(0, 1, "A")})
    assert decide_chains(chains) == Decision(links=[("a", "b"), ("b", "c")])
    assert chains.links == {("a", "b"), ("b", "c")}
    assert chains.premises == {}
    assert decide_chains(chains) == Decision()


@pytest.mark.parametrize("reverse", [False, True])
def test_ambiguous_candidate_order_uses_sightings_then_labels(reverse):
    pairs = [("a", span(0, 1, "Z")), ("b", span(0, 1, "A")), ("c", span(3, 4, "C"))]
    chains = Chains(spans=dict(reversed(pairs) if reverse else pairs))
    assert decide_chains(chains) == Decision(
        ambiguous=[
            {
                "source": "C",
                "fingerprint": "c",
                "first_seen": (T0 + timedelta(hours=3)).isoformat(),
                "why": "more than one source of its class ended before it",
                "candidates": [
                    {"source": "A", "fingerprint": "b"},
                    {"source": "Z", "fingerprint": "a"},
                ],
            }
        ]
    )
    assert chains.links == set() and chains.premises == {}


def test_incomplete_history_prevents_the_whole_group_from_linking():
    chains = Chains(spans={"a": span(0, 1, "A"), "b": span(3, 4, "B", complete=False)})
    assert decide_chains(chains) == Decision(
        ambiguous=[
            {
                "source": "B",
                "fingerprint": "b",
                "first_seen": (T0 + timedelta(hours=3)).isoformat(),
                "why": "history incomplete: run identity-backfill (a full run) first",
                "candidates": [],
            }
        ]
    )
    assert chains.links == set()


@pytest.mark.parametrize("decider", ["owner", "backfill", "unknown"])
async def test_verification_keeps_all_chain_diagnostics_in_order(sessions, decider):
    async with sessions.begin() as session:
        session.add(
            RawSourceChain(
                data_type="heart-rate",
                from_fingerprint="a",
                to_fingerprint="b",
                from_label="A",
                to_label="B",
                decided_by=decider,
                evidence="{}",
                decided_at=T0,
            )
        )
    name = "source chain of heart-rate a -> b"
    expected = [
        "raw_source_devices of heart-rate does not match its live chains for 2 source(s) "
        "(e.g. a): readers would count supersessions wrongly",
    ]
    if decider == "unknown":
        expected.append(f"{name} was not decided by the reconciler ('unknown')")
    expected.append(f"{name} has no evidence that satisfies the chain rule")
    if decider == "owner":
        expected.append(f"{name} names no owner decision to link them")
    expected.append(f"{name} has no sightings for both sources (they were removed)")
    async with sessions() as session:
        assert await verify_identities(session) == (0, expected, [])
        assert not session.new and not session.dirty and not session.deleted
