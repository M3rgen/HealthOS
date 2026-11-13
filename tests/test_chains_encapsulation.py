"""Source-chain state is updated through its API, including warmed query caches."""

import operator
from datetime import timedelta

import pytest
from identity_records import HEART, T0

from app.models.identity_tables import RawSourcePair
from app.services.identities import source_chain_repository as repository
from app.services.identities.source_chain_evidence import Evidence, Span
from app.services.identities.source_chain_model import Chains

PAIR = ("a", "b")
MAPPINGS = (
    "spans",
    "decided_by",
    "evidence",
    "chain_decisions",
    "pair_decisions",
    "logged_devices",
    "agreement",
    "premises",
    "following",
    "preceding",
)


@pytest.mark.parametrize("name", (*MAPPINGS, "links", "refuted_events"))
def test_state_cannot_be_replaced_or_mutated_through_a_reader(name):
    chains = Chains(links={PAIR}, evidence={PAIR: [Evidence("key", T0)]})
    with pytest.raises(AttributeError):
        setattr(chains, name, {})
    value = getattr(chains, name)
    if name in MAPPINGS:
        with pytest.raises(TypeError):
            operator.setitem(value, "unrecorded", "value")
    else:
        with pytest.raises(AttributeError):
            value.add(("unrecorded", "link"))
    with pytest.raises(AttributeError):
        chains.evidence[PAIR].append(Evidence("unrecorded", T0))
    assert chains.following == {"a": "b"}
    assert chains.same_device(*PAIR)


def test_constructor_owns_collections_and_nested_evidence():
    links, refuted = {PAIR}, {("c", "d")}
    items = [Evidence("key", T0)]
    inputs = {
        "spans": {"a": Span(T0, T0, "band", "A", False)},
        "decided_by": {PAIR: "owner"},
        "evidence": {PAIR: items},
        "chain_decisions": {PAIR: "confirm"},
        "pair_decisions": {PAIR: "not-distinct"},
        "logged_devices": {"a": "a"},
        "agreement": {PAIR: (3, 0, True)},
        "premises": {PAIR: frozenset({("c", "d")})},
    }
    chains = Chains(links=links, refuted_events=refuted, **inputs)
    expected = {name: dict(getattr(chains, name)) for name in inputs}
    links.clear()
    refuted.clear()
    items.clear()
    for value in inputs.values():
        value.clear()
    assert chains.links == {PAIR} and chains.refuted_events == {("c", "d")}
    assert {name: dict(getattr(chains, name)) for name in inputs} == expected
    assert chains.evidence[PAIR] == (Evidence("key", T0),)


def test_link_updates_invalidate_live_maps_membership_and_recorded_components():
    chains = Chains()
    assert chains.following == chains.preceding == {}
    assert not chains.same_device(*PAIR) and not chains.ever_joined(*PAIR)
    chains.add(*PAIR, decided_by="owner")
    assert chains.following == {"a": "b"} and chains.preceding == {"b": "a"}
    assert chains.same_device(*PAIR) and chains.ever_joined(*PAIR)
    assert chains.review(*PAIR) == "owner"
    chains.forget(*PAIR)
    assert chains.following == chains.preceding == {}
    assert not chains.same_device(*PAIR) and not chains.ever_joined(*PAIR)


def test_premises_and_refutations_invalidate_dependent_links():
    chains = Chains(links={PAIR, ("c", "d")})
    assert chains.live("c", "d") and chains.same_device("c", "d")
    chains.set_premise("c", "d", frozenset({PAIR}))
    assert chains.premise_holds("c", "d") and chains.following == {"a": "b", "c": "d"}
    chains.refute([PAIR])
    assert chains.following == chains.preceding == {}
    assert not chains.premise_holds("c", "d") and not chains.same_device("c", "d")
    assert chains.ever_joined(*PAIR)
    chains.decide("not-distinct", "b", "a")
    assert chains.same_device(*PAIR) and chains.same_device("c", "d")
    chains.decide("reject", *PAIR)
    assert not chains.live("c", "d")
    chains.decide("confirm", "c", "d")
    assert chains.same_device("c", "d") and chains.review("c", "d") == "confirmed"


@pytest.mark.parametrize("recorded", [False, True])
def test_evidence_and_agreement_updates_invalidate_distinctness(recorded):
    chains = Chains()
    assert not chains.distinct(*PAIR)
    for index in range(3):
        seen = T0 + timedelta(hours=index)
        item = Evidence(f"v2:{seen:%Y%m%dT%H%M%S}:sample", seen)
        if recorded:
            chains.load_evidence(*PAIR, item)
        else:
            assert chains.add_evidence("b", "a", item.key, item.seen)
    assert chains.distinct(*PAIR)
    assert not chains.add_evidence(*PAIR, item.key, item.seen)
    chains.update_agreement({PAIR: (3, 3, True)})
    assert not chains.distinct(*PAIR) and chains.agreement_counted(*PAIR)
    chains.decide("distinct", "b", "a")
    assert chains.distinct(*PAIR)


def test_spans_devices_and_copy_keep_updates_local_to_their_state():
    chains = Chains(links={PAIR})
    incomplete = Span(T0, T0, "band", "A", False)
    chains.set_span("a", incomplete)
    chains.complete_spans()
    assert chains.spans["a"].complete
    assert chains.devices_to_log() == {"a": "a", "b": "a"}
    chains.log_devices({"a": "a", "b": "a"})
    assert chains.devices_to_log() == {}
    copied = chains.copy()
    assert copied.same_device(*PAIR)
    copied.refute([PAIR])
    copied.set_span("a", Span(T0, T0, "band", "replacement", True))
    copied.log_devices({"b": "b"})
    copied.load_evidence(*PAIR, Evidence("key", T0))
    assert not copied.same_device(*PAIR) and chains.same_device(*PAIR)
    assert chains.spans["a"] is incomplete and chains.logged_devices["b"] == "a"
    assert chains.evidence == {} and chains.refuted_events == set()


async def test_repository_loads_all_recorded_evidence_without_resampling(sessions):
    # Legacy recorded evidence can exceed the new-evidence sampling bound. Loading it
    # must preserve every row, including the later key that establishes distinctness.
    items = [Evidence(f"legacy-{index}", T0) for index in range(4)]
    later = T0 + timedelta(hours=1)
    items.append(Evidence(f"v2:{later:%Y%m%dT%H%M%S}:sample", later))
    async with sessions.begin() as session:
        session.add_all(
            RawSourcePair(
                data_type=HEART.name,
                first_fingerprint="a",
                second_fingerprint="b",
                identity_key=item.key,
                seen_together_at=item.seen,
            )
            for item in items
        )
    async with sessions() as session:
        chains = (await repository.load_state(session, HEART.name)).chains
        assert set(chains.evidence[PAIR]) == set(items)
        assert chains.distinct(*PAIR)
