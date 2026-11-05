"""The H2H observer must validate identities bound to historical edges."""
from dataclasses import replace

import pytest

from autosport.opponent_intelligence import OpponentIntelligenceStore
from autosport.participant_identity import (
    AliasRecord, EntityIdentity, EntityKind, IdentityView, ParticipantIdentityRegistry,
)
from causal_h2h_observer import feature
from test_causal_h2h_observer import SHA, market, observed, stamp, store_fixture


@pytest.mark.parametrize("unconfirmed", ["A", "B", "L"])
def test_historical_candidate_binding_is_never_ready(tmp_path, unconfirmed):
    registry, store = store_fixture(tmp_path)
    # Current market aliases are confirmed; one historical source binding is not.
    for name in ("A", "B", "L"):
        registry.add_alias(AliasRecord("other", name, name, stamp(0), None,
                                       stamp(0), SHA, stamp(0),
                                       relation="CANDIDATE" if name == unconfirmed else "CONFIRMED"))
    store.record_performance(observed(source="other"))
    result = feature("current", market(), stamp(31), registry, store)
    assert result["h2h_status"] == "history_identity_unconfirmed"
    assert result["prior_meetings"] is None and result["h2h_home_probability"] is None


def test_historical_alias_correction_blocks_even_without_published_invalidation(tmp_path):
    registry, store = store_fixture(tmp_path)
    registry.add_alias(AliasRecord("moviodds.com", "oldA", "A", stamp(0), None,
                                   stamp(0), SHA, stamp(0)))
    previous = registry.resolve_alias_record("moviodds.com", "oldA", as_of=stamp(31))
    store.record_performance(observed(subject="oldA"))
    registry.add_entity(EntityIdentity("A2", EntityKind.PARTICIPANT, "test:A2", SHA, stamp(0), stamp(0)))
    registry.add_alias(AliasRecord("moviodds.com", "oldA", "A2", stamp(0), None,
                                   stamp(40), SHA, stamp(40), supersedes_record_id=previous.record_id))
    # No refresh_identity_invalidations call: the registry is the authority.
    assert feature("current", market(), stamp(40), registry, store)["h2h_status"] == "ready"
    after = feature("current", market(), stamp(41), registry, store)
    assert after["h2h_status"] == "history_identity_unreconciled"
    assert after["prior_meetings"] is None and after["h2h_home_probability"] is None


def test_expired_historical_alias_is_conservatively_unreconciled(tmp_path):
    registry, store = store_fixture(tmp_path)
    for name in ("A", "B", "L"):
        until = stamp(35) if name == "A" else None
        registry.add_alias(AliasRecord("other", name, name, stamp(0), until,
                                       stamp(0), SHA, stamp(0)))
    store.record_performance(observed(source="other"))
    assert feature("current", market(), stamp(31), registry, store)["h2h_status"] == "ready"
    after = feature("current", market(), stamp(36), registry, store)
    assert after["h2h_status"] == "history_identity_unreconciled"
    assert after["prior_meetings"] is None


def test_future_or_current_fixture_unconfirmed_record_is_not_prior_history(tmp_path):
    registry, store = store_fixture(tmp_path)
    for name in ("A", "B", "L"):
        registry.add_alias(AliasRecord("other", name, name, stamp(0), None,
                                       stamp(0), SHA, stamp(0), relation="CANDIDATE"))
    store.record_performance(observed(event="current", source="other"))
    store.record_performance(observed(event="future", source="other", available=40, recorded=40))
    assert feature("current", market(), stamp(40), registry, store)["h2h_status"] == "no_prior_history"
    assert feature("current", market(), stamp(41), registry, store)["h2h_status"] == "history_identity_unconfirmed"


def test_unrelated_unconfirmed_edge_does_not_hide_confirmed_meeting(tmp_path):
    registry, store = store_fixture(tmp_path)
    registry.add_entity(EntityIdentity("C", EntityKind.PARTICIPANT, "test:C", SHA, stamp(0), stamp(0)))
    for name in ("C", "B", "L"):
        registry.add_alias(AliasRecord("other", name, name, stamp(0), None,
                                       stamp(0), SHA, stamp(0),
                                       relation="CANDIDATE" if name == "C" else "CONFIRMED"))
    store.record_performance(observed(event="unrelated", subject="C", source="other"))
    store.record_performance(observed())
    result = feature("current", market(), stamp(31), registry, store)
    assert (result["h2h_status"], result["prior_meetings"], result["prior_home_wins"]) == ("ready", 1, 1)


def test_one_unconfirmed_meeting_blocks_whole_h2h_not_just_one_row(tmp_path):
    registry, store = store_fixture(tmp_path)
    for name in ("A", "B", "L"):
        registry.add_alias(AliasRecord("other", name, name, stamp(0), None,
                                       stamp(0), SHA, stamp(0),
                                       relation="CANDIDATE" if name == "A" else "CONFIRMED"))
    store.record_performance(observed())
    store.record_performance(observed(event="second", source="other"))
    result = feature("current", market(), stamp(31), registry, store)
    assert result["h2h_status"] == "history_identity_unconfirmed"
    assert result["prior_meetings"] is None and result["h2h_home_probability"] is None


def test_restated_edge_cannot_backdate_entity_availability(tmp_path):
    registry = ParticipantIdentityRegistry.initialize_pristine(tmp_path / "identity.json")
    for name, kind in (("A", EntityKind.PARTICIPANT), ("B", EntityKind.PARTICIPANT),
                       ("L", EntityKind.LEAGUE)):
        registry.add_entity(EntityIdentity(name, kind, "test:" + name, SHA, stamp(0),
                                           stamp(35) if name == "A" else stamp(0)))
        for source in ("moviodds.com", "other"):
            registry.add_alias(AliasRecord(source, name, name, stamp(0), None,
                                           stamp(0), SHA, stamp(0)))
    store = OpponentIntelligenceStore.initialize_pristine(tmp_path / "opponents.json", registry)
    # RESTATED may bind an entity not available at the historical observation.
    store.record_performance(observed(source="other"), identity_view=IdentityView.RESTATED_RESEARCH)
    result = feature("current", market(), stamp(41), registry, store)
    assert result["h2h_status"] == "history_identity_unreconciled"
    assert result["prior_meetings"] is None and result["h2h_home_probability"] is None


@pytest.mark.parametrize("changed", ["participant", "league"])
@pytest.mark.parametrize("with_valid_meeting", [False, True])
def test_known_correction_can_make_previously_unrelated_edge_relevant(tmp_path, changed, with_valid_meeting):
    registry, store = store_fixture(tmp_path)
    for name in ("A", "B", "L"):
        registry.add_alias(AliasRecord("other", name, name, stamp(0), None,
                                       stamp(0), SHA, stamp(0)))
    if changed == "participant":
        registry.add_entity(EntityIdentity("C", EntityKind.PARTICIPANT, "test:C", SHA, stamp(0), stamp(0)))
        registry.add_alias(AliasRecord("other", "old", "C", stamp(0), None, stamp(0), SHA, stamp(0)))
        old = registry.resolve_alias_record("other", "old", as_of=stamp(31))
        store.record_performance(observed(subject="old", source="other"))
        corrected_entity = "A"
    else:
        registry.add_entity(EntityIdentity("L2", EntityKind.LEAGUE, "test:L2", SHA, stamp(0), stamp(0)))
        registry.add_alias(AliasRecord("other", "old", "L2", stamp(0), None, stamp(0), SHA, stamp(0)))
        old = registry.resolve_alias_record("other", "old", as_of=stamp(31))
        store.record_performance(replace(observed(source="other"), league_alias="old"))
        corrected_entity = "L"
    registry.add_alias(AliasRecord("other", "old", corrected_entity, stamp(0), None,
                                   stamp(40), SHA, stamp(40), supersedes_record_id=old.record_id))
    if with_valid_meeting:
        store.record_performance(observed(event="confirmed"))
    before = feature("current", market(), stamp(40), registry, store)
    assert before["h2h_status"] == ("ready" if with_valid_meeting else "no_prior_history")
    result = feature("current", market(), stamp(41), registry, store)
    assert result["h2h_status"] == "history_identity_unreconciled"
    assert result["prior_meetings"] is None and result["h2h_home_probability"] is None
