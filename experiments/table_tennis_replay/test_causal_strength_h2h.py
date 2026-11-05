from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import hashlib
import json

import pytest

from autosport.participant_identity import AliasRecord, EntityIdentity, EntityKind, EntityLineage, LineageRelation
from causal_strength_h2h import MAX_AGE_SECONDS, compare
from causal_h2h_observer import feature
from delay_policy_audit import _exact_snapshot
from replay_empty_strength import replay
from test_causal_h2h_observer import SHA, market, observed, stamp, store_fixture
from test_quote_delay_audit import BASE, build_session, match


def setup(root):
    registry, store = store_fixture(root)
    for name in ("C", "D", "E", "F", "G"):
        registry.add_entity(EntityIdentity(name, EntityKind.PARTICIPANT, "test:" + name, SHA, stamp(0), stamp(0)))
        registry.add_alias(AliasRecord("moviodds.com", name, name, stamp(0), None,
                                       stamp(0), SHA, stamp(0)))
    return registry, store


def history(store, *, with_h2h=False):
    for event, a, b, score in (("ac", "A", "C", "1"), ("ad", "A", "D", "1"),
                                ("be", "B", "E", "0"), ("bf", "B", "F", "0")):
        store.record_performance(observed(event=event, subject=a, opponent=b, score=score))
    if with_h2h:
        store.record_performance(observed(event="ab", subject="A", opponent="B", score="1"))


def test_form_and_h2h_use_canonical_support_and_distinct_input_edges(tmp_path):
    registry, store = setup(tmp_path)
    history(store, with_h2h=True)
    before = (tmp_path / "identities.json").read_bytes(), (tmp_path / "opponents.json").read_bytes()
    result = compare("current", market(), stamp(31), registry, store)
    assert result["market_home_probability"] == "0.625"
    assert result["h2h_status"] == "ready" and result["h2h_support"] == 1
    assert result["strength_status"] == "ready"
    assert (result["home_form"]["support"], result["home_form"]["effective_sample"],
            result["home_form"]["mean_score"]) == (2, 2, "1")
    assert (result["away_form"]["support"], result["away_form"]["effective_sample"],
            result["away_form"]["mean_score"]) == (2, 2, "0")
    assert abs(Decimal(result["strength_home_probability"]) - Decimal(9) / Decimal(14)) < Decimal("1e-26")
    assert abs(Decimal(result["strength_h2h_home_probability"]) - Decimal(59) / Decimal(84)) < Decimal("1e-26")
    assert result["home_form"]["support_radius_not_ci"] is not None
    assert result["model_training"] is result["bookmaker_execution"] is result["calibration_verified"] is False
    assert before == ((tmp_path / "identities.json").read_bytes(), (tmp_path / "opponents.json").read_bytes())


def test_no_h2h_returns_form_but_no_combined_value(tmp_path):
    registry, store = setup(tmp_path)
    history(store)
    result = compare("current", market(), stamp(31), registry, store)
    assert result["h2h_status"] == "no_prior_history" and result["h2h_support"] == 0
    assert result["strength_status"] == "ready"
    assert result["strength_h2h_home_probability"] is None


def test_future_and_same_instant_results_are_not_form_inputs(tmp_path):
    registry, store = setup(tmp_path)
    history(store)
    at = compare("current", market(), stamp(30), registry, store)
    after = compare("current", market(), stamp(31), registry, store)
    assert at["strength_status"] == "insufficient_or_stale"
    assert at["home_form"]["support"] == at["away_form"]["support"] == 0
    assert after["strength_status"] == "ready"


def test_result_correction_changes_only_later_as_known_form(tmp_path):
    registry, store = setup(tmp_path)
    original = store.record_performance(observed(event="ac", subject="A", opponent="C", score="1"))
    for event, a, b, score in (("ad", "A", "D", "1"), ("be", "B", "E", "0"), ("bf", "B", "F", "0")):
        store.record_performance(observed(event=event, subject=a, opponent=b, score=score))
    store.record_performance(observed(event="ac", subject="A", opponent="C", score="0",
                                      available=40, recorded=40, supersedes=original.performance_id))
    before = compare("current", market(), stamp(40), registry, store)
    after = compare("current", market(), stamp(41), registry, store)
    assert before["home_form"]["mean_score"] == "1"
    assert after["home_form"]["mean_score"] == "0.5"
    assert Decimal(after["strength_home_probability"]) < Decimal(before["strength_home_probability"])
    assert compare("current", market(), stamp(40), registry, store) == before


def test_reversed_sides_reverse_both_heuristics(tmp_path):
    registry, store = setup(tmp_path)
    history(store, with_h2h=True)
    original = compare("current", market(), stamp(31), registry, store)
    h, a, league, quotes = market()
    reversed_result = compare("current", (a, h, league, quotes), stamp(31), registry, store)
    for key in ("market_home_probability", "strength_home_probability", "strength_h2h_home_probability"):
        assert abs(Decimal(original[key]) + Decimal(reversed_result[key]) - 1) < Decimal("1e-26")


def test_current_fixture_and_h2h_are_not_in_form_support(tmp_path):
    registry, store = setup(tmp_path)
    history(store, with_h2h=True)
    store.record_performance(observed(event="current", subject="A", opponent="G", score="0"))
    result = compare("current", market(), stamp(31), registry, store)
    assert (result["home_form"]["support"], result["away_form"]["support"]) == (2, 2)
    assert result["h2h_support"] == 1


def test_repeated_same_opponent_cannot_inflate_effective_support(tmp_path):
    registry, store = setup(tmp_path)
    store.record_performance(observed(event="ac1", subject="A", opponent="C", score="1"))
    store.record_performance(observed(event="ac2", subject="A", opponent="C", score="1"))
    store.record_performance(observed(event="be", subject="B", opponent="E", score="0"))
    store.record_performance(observed(event="bf", subject="B", opponent="F", score="0"))
    result = compare("current", market(), stamp(31), registry, store)
    assert result["home_form"]["support"] == 2 and result["home_form"]["effective_sample"] == 1
    assert result["strength_status"] == "insufficient_or_stale"
    assert result["strength_home_probability"] is None


def test_exact_freshness_boundary_is_checked_before_publishing(tmp_path):
    registry, store = setup(tmp_path)
    history(store)
    # Last observed_at is BASE+10s; subtracting one microsecond gives
    # a causal cutoff exactly at the boundary for the first invocation.
    at = BASE + timedelta(seconds=10 + MAX_AGE_SECONDS, microseconds=1)
    later = at + timedelta(microseconds=1)
    assert compare("current", market(), at.isoformat(), registry, store)["strength_status"] == "ready"
    stale = compare("current", market(), later.isoformat(), registry, store)
    assert stale["strength_status"] == "insufficient_or_stale"
    assert stale["home_form"]["within_exact_max_age"] is False
    assert stale["home_form"]["mean_score"] is None


def test_missing_identity_blocks_all_history_candidates(tmp_path):
    registry, store = setup(tmp_path)
    history(store)
    h, a, league, quotes = market()
    missing = compare("current", (h, "unknown", league, {h: quotes[h], "unknown": quotes[a]}),
                      stamp(31), registry, store)
    assert missing["h2h_status"] == "identity_unresolved"
    assert missing["strength_status"] == "blocked_by_identity_or_h2h_history"
    assert missing["strength_home_probability"] is None


def test_unconfirmed_historical_opponent_blocks_form(tmp_path):
    registry, store = setup(tmp_path)
    for name in ("A", "B", "C", "D", "E", "F", "L"):
        registry.add_alias(AliasRecord("other", name, name, stamp(0), None, stamp(0), SHA, stamp(0),
                                       relation="CANDIDATE" if name == "C" else "CONFIRMED"))
    store.record_performance(observed(event="ac", subject="A", opponent="C", source="other"))
    result = compare("current", market(), stamp(31), registry, store)
    assert result["strength_status"] == "history_identity_unconfirmed"
    assert result["strength_home_probability"] is None


def test_known_correction_can_make_earlier_unrelated_form_relevant(tmp_path):
    registry, store = setup(tmp_path)
    history(store)
    registry.add_alias(AliasRecord("moviodds.com", "oldC", "C", stamp(0), None, stamp(0), SHA, stamp(0)))
    previous = registry.resolve_alias_record("moviodds.com", "oldC", as_of=stamp(31))
    store.record_performance(observed(event="outside", subject="oldC", opponent="D"))
    registry.add_alias(AliasRecord("moviodds.com", "oldC", "A", stamp(0), None, stamp(40), SHA, stamp(40),
                                   supersedes_record_id=previous.record_id))
    assert compare("current", market(), stamp(40), registry, store)["strength_status"] == "ready"
    assert compare("current", market(), stamp(41), registry, store)["strength_status"] == "history_identity_unreconciled"


def test_relevant_historical_alias_correction_blocks_form_before_and_after_invalidation_refresh(tmp_path):
    registry, store = setup(tmp_path)
    history(store)
    registry.add_alias(AliasRecord("moviodds.com", "oldC", "C", stamp(0), None, stamp(0), SHA, stamp(0)))
    previous = registry.resolve_alias_record("moviodds.com", "oldC", as_of=stamp(31))
    store.record_performance(observed(event="additional", subject="A", opponent="oldC"))
    registry.add_alias(AliasRecord("moviodds.com", "oldC", "G", stamp(0), None, stamp(40), SHA, stamp(40),
                                   supersedes_record_id=previous.record_id))
    assert compare("current", market(), stamp(40), registry, store)["strength_status"] == "ready"
    assert compare("current", market(), stamp(41), registry, store)["strength_status"] == "history_identity_unreconciled"
    store.refresh_identity_invalidations(detected_at=stamp(40))
    assert compare("current", market(), stamp(41), registry, store)["strength_status"] == "history_identity_invalidated"


def test_opponent_lineage_requires_reconciliation(tmp_path):
    registry, store = setup(tmp_path)
    history(store)
    registry.add_lineage(EntityLineage("C", "G", LineageRelation.MERGED_FROM,
                                       stamp(35), stamp(35), stamp(35), SHA))
    assert compare("current", market(), stamp(35), registry, store)["strength_status"] == "ready"
    assert compare("current", market(), stamp(36), registry, store)["strength_status"] == "history_identity_lineage_unreconciled"


@pytest.mark.parametrize("kind", ["duplicate", "nonbinary"])
def test_ambiguous_history_fails_closed(tmp_path, kind):
    registry, store = setup(tmp_path)
    store.record_performance(observed(event="ac", subject="A", opponent="C",
                                      score="0.5" if kind == "nonbinary" else "1"))
    if kind == "duplicate":
        store.record_performance(observed(event="ac", subject="B", opponent="D"))
    result = compare("current", market(), stamp(31), registry, store)
    assert result["strength_status"] == "history_ambiguous"
    assert result["strength_home_probability"] is None


def test_other_sport_and_league_are_not_form_history(tmp_path):
    registry, store = setup(tmp_path)
    registry.add_entity(EntityIdentity("L2", EntityKind.LEAGUE, "test:L2", SHA, stamp(0), stamp(0)))
    registry.add_alias(AliasRecord("moviodds.com", "L2", "L2", stamp(0), None, stamp(0), SHA, stamp(0)))
    store.record_performance(replace(observed(event="soccer", subject="A", opponent="C"), sport_id="soccer"))
    store.record_performance(replace(observed(event="other-league", subject="B", opponent="E"), league_alias="L2"))
    result = compare("current", market(), stamp(31), registry, store)
    assert result["home_form"]["support"] == 0 and result["away_form"]["support"] == 0


def test_empty_store_replay_checks_pins_and_never_produces_form(tmp_path):
    # Create truly empty fixture authorities for the replay.
    from autosport.opponent_intelligence import OpponentIntelligenceStore
    from autosport.participant_identity import ParticipantIdentityRegistry
    empty_registry = ParticipantIdentityRegistry.initialize_pristine(tmp_path / "identity-empty.json")
    OpponentIntelligenceStore.initialize_pristine(tmp_path / "opponents-empty.json", empty_registry)
    session = tmp_path / "snapshots"
    build_session(session, [(0, [match("a", 0)]), (40, [match("a", 40)])])
    for kind in ("odds", "results"):
        for part in ("body", "receipt"):
            (session / f"0002.{kind}.{part}.json").unlink()
    odds = json.loads((session / "0001.odds.receipt.json").read_text())
    state = _exact_snapshot(session, 1, odds)["a"]
    empty_store = OpponentIntelligenceStore(tmp_path / "opponents-empty.json", empty_registry)
    forecast = feature("a", state, stamp(1.25), empty_registry, empty_store)
    journal = {"feature_cutoff": stamp(1.25), "odds_sha256": odds["sha256"],
               "prior_result_sha256": SHA, "forecasts": [forecast]}
    raw = json.dumps(journal).encode()
    digest = hashlib.sha256(raw).hexdigest()
    envelope = {"schema": "research.tt_forecast_receipt.v1", "bytes": len(raw), "sha256": digest,
                "odds_sha256": odds["sha256"], "prior_result_sha256": SHA,
                "computed_at": stamp(1.25), "frozen_at": stamp(1.5)}
    (tmp_path / "forecast.body.json").write_bytes(raw)
    (tmp_path / "forecast.receipt.json").write_text(json.dumps(envelope))
    result = replay(tmp_path, expected_forecast_sha256=digest)
    assert result["frozen_forecasts"] == 1 and result["new_strength_values"] == 0
    assert result["status_counts"]["identity_unresolved"] == 1 and result["stores_unchanged"]
    with pytest.raises(ValueError, match="externally pinned"):
        replay(tmp_path, expected_forecast_sha256="0" * 64)
    empty_registry.add_entity(EntityIdentity("X", EntityKind.PARTICIPANT, "test:X", SHA, stamp(0), stamp(0)))
    with pytest.raises(ValueError, match="empty identity"):
        replay(tmp_path, expected_forecast_sha256=digest)
