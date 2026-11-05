from datetime import timedelta
from decimal import Decimal
import hashlib
import json

import pytest

import causal_h2h_observer
from autosport.learning_environment import EvidenceTruth
from autosport.opponent_intelligence import ObservedPerformance, OpponentIntelligenceStore
from autosport.participant_identity import (
    AliasRecord, EntityIdentity, EntityKind, EntityLineage, LineageRelation, ParticipantIdentityRegistry,
)
from causal_h2h_observer import build_journal, feature, prepare_prior, run, verify_journal
from prospective_capture import capture_one
from test_delayed_portfolio_replay import followup, later_result
from test_quote_delay_audit import BASE, build_session, match, stamp


SHA = "a" * 64


def store_fixture(root):
    root.mkdir(exist_ok=True)
    registry = ParticipantIdentityRegistry.initialize_pristine(root / "identities.json")
    for alias, kind in (("A", EntityKind.PARTICIPANT), ("B", EntityKind.PARTICIPANT),
                        ("L", EntityKind.LEAGUE)):
        registry.add_entity(EntityIdentity(alias, kind, "test:" + alias, SHA, stamp(0), stamp(0)))
        registry.add_alias(AliasRecord("moviodds.com", alias, alias, stamp(0), None,
                                       stamp(0), SHA, stamp(0)))
    return registry, OpponentIntelligenceStore.initialize_pristine(root / "opponents.json", registry)


def market():
    return ("A", "B", "L", {"A": ("1", Decimal("1.5")), "B": ("2", Decimal("2.5"))})


def observed(event="previous", subject="A", opponent="B", score="1", *, available=20,
             recorded=30, source="moviodds.com", supersedes=None):
    return ObservedPerformance(event_id=event, source_id=source, subject_alias=subject,
                               opponent_alias=opponent, sport_id="table_tennis", league_alias="L",
                               market_context_id="match-outcome", score=score,
                               observed_at=stamp(10), available_at=stamp(available),
                               recorded_at=stamp(recorded), evidence_sha256=SHA,
                               truth=EvidenceTruth.OBSERVED, supersedes_performance_id=supersedes)


def test_no_identity_never_becomes_training_or_h2h(tmp_path):
    registry = ParticipantIdentityRegistry.initialize_pristine(tmp_path / "identity.json")
    store = OpponentIntelligenceStore.initialize_pristine(tmp_path / "opponents.json", registry)
    result = feature("current", market(), stamp(31), registry, store)
    assert result["market_home_probability"] == "0.625"
    assert result["h2h_status"] == "identity_unresolved"
    assert result["prior_meetings"] is result["h2h_home_probability"] is None


def test_recorded_and_available_time_are_strictly_before_cutoff(tmp_path):
    registry, store = store_fixture(tmp_path)
    store.record_performance(observed())
    same = feature("current", market(), stamp(30), registry, store)
    after = feature("current", market(), stamp(31), registry, store)
    assert same["h2h_status"] == "no_prior_history" and same["prior_meetings"] == 0
    assert after["prior_meetings"] == after["prior_home_wins"] == 1
    assert after["h2h_home_probability"] == "0.6875"


def test_reversed_subject_and_current_fixture_exclusion(tmp_path):
    registry, store = store_fixture(tmp_path)
    store.record_performance(observed(event="previous", subject="B", opponent="A", score="0"))
    store.record_performance(observed(event="current", subject="A", opponent="B", score="0"))
    result = feature("current", market(), stamp(31), registry, store)
    assert result["prior_meetings"] == result["prior_home_wins"] == 1


def test_correction_changes_only_subsequent_as_known_state(tmp_path):
    registry, store = store_fixture(tmp_path)
    original = store.record_performance(observed())
    store.record_performance(observed(score="0", available=40, recorded=40,
                                      supersedes=original.performance_id))
    before = feature("current", market(), stamp(40), registry, store)
    after = feature("current", market(), stamp(41), registry, store)
    assert (before["prior_meetings"], before["prior_home_wins"]) == (1, 1)
    assert (after["prior_meetings"], after["prior_home_wins"]) == (1, 0)


def test_nonbinary_and_duplicate_event_history_fail_closed(tmp_path):
    registry, store = store_fixture(tmp_path / "first")
    store.record_performance(observed(score="0.5"))
    assert feature("current", market(), stamp(31), registry, store)["h2h_status"] == "history_ambiguous"
    registry, store = store_fixture(tmp_path / "second")
    for alias in ("A", "B", "L"):
        registry.add_alias(AliasRecord("other", alias, alias, stamp(0), None, stamp(0), SHA, stamp(0)))
    store.record_performance(observed())
    store.record_performance(observed(source="other"))
    assert feature("current", market(), stamp(31), registry, store)["h2h_status"] == "history_ambiguous"


def test_alias_available_only_at_cutoff_stays_unresolved(tmp_path):
    registry = ParticipantIdentityRegistry.initialize_pristine(tmp_path / "identity.json")
    for alias, kind in (("A", EntityKind.PARTICIPANT), ("B", EntityKind.PARTICIPANT),
                        ("L", EntityKind.LEAGUE)):
        registry.add_entity(EntityIdentity(alias, kind, "test:" + alias, SHA, stamp(0), stamp(0)))
        registry.add_alias(AliasRecord("moviodds.com", alias, alias, stamp(0), None,
                                       stamp(30), SHA, stamp(30)))
    store = OpponentIntelligenceStore.initialize_pristine(tmp_path / "opponents.json", registry)
    assert feature("current", market(), stamp(30), registry, store)["h2h_status"] == "identity_unresolved"
    assert feature("current", market(), stamp(31), registry, store)["h2h_status"] == "no_prior_history"


def test_candidate_alias_is_not_a_confirmed_identity(tmp_path):
    registry = ParticipantIdentityRegistry.initialize_pristine(tmp_path / "identity.json")
    for alias, kind in (("A", EntityKind.PARTICIPANT), ("B", EntityKind.PARTICIPANT),
                        ("L", EntityKind.LEAGUE)):
        registry.add_entity(EntityIdentity(alias, kind, "test:" + alias, SHA, stamp(0), stamp(0)))
        registry.add_alias(AliasRecord("moviodds.com", alias, alias, stamp(0), None,
                                       stamp(0), SHA, stamp(0), relation="CANDIDATE"))
    store = OpponentIntelligenceStore.initialize_pristine(tmp_path / "opponents.json", registry)
    result = feature("current", market(), stamp(31), registry, store)
    assert result["h2h_status"] == "identity_not_confirmed"
    assert result["h2h_home_probability"] is None


def test_identity_alias_correction_is_not_silently_applied_to_past_history(tmp_path):
    registry, store = store_fixture(tmp_path)
    store.record_performance(observed())
    previous = registry.resolve_alias_record("moviodds.com", "A", as_of=stamp(31))
    registry.add_entity(EntityIdentity("A2", EntityKind.PARTICIPANT, "test:A2", SHA, stamp(0), stamp(0)))
    registry.add_alias(AliasRecord("moviodds.com", "A", "A2", stamp(0), None,
                                   stamp(40), SHA, stamp(40), supersedes_record_id=previous.record_id))
    before = feature("current", market(), stamp(40), registry, store)
    after = feature("current", market(), stamp(41), registry, store)
    assert before["h2h_status"] == "ready" and before["prior_home_wins"] == 1
    assert after["h2h_status"] == "identity_revision_unreconciled"
    assert after["prior_meetings"] is after["h2h_home_probability"] is None


def test_known_identity_lineage_blocks_h2h_even_without_refreshed_store(tmp_path):
    registry, store = store_fixture(tmp_path)
    store.record_performance(observed())
    registry.add_entity(EntityIdentity("A2", EntityKind.PARTICIPANT, "test:A2", SHA, stamp(0), stamp(0)))
    registry.add_lineage(EntityLineage("A", "A2", LineageRelation.MERGED_FROM,
                                       stamp(35), stamp(35), stamp(35), SHA))
    assert feature("current", market(), stamp(35), registry, store)["h2h_status"] == "ready"
    assert feature("current", market(), stamp(36), registry, store)["h2h_status"] == "identity_lineage_unreconciled"


def test_published_identity_invalidation_blocks_affected_edge_after_detection(tmp_path):
    registry, store = store_fixture(tmp_path)
    registry.add_alias(AliasRecord("moviodds.com", "oldA", "A", stamp(0), None,
                                   stamp(0), SHA, stamp(0)))
    previous = registry.resolve_alias_record("moviodds.com", "oldA", as_of=stamp(31))
    store.record_performance(observed(subject="oldA"))
    registry.add_entity(EntityIdentity("A2", EntityKind.PARTICIPANT, "test:A2", SHA, stamp(0), stamp(0)))
    registry.add_alias(AliasRecord("moviodds.com", "oldA", "A2", stamp(0), None,
                                   stamp(40), SHA, stamp(40), supersedes_record_id=previous.record_id))
    store.refresh_identity_invalidations(detected_at=stamp(40))
    assert feature("current", market(), stamp(40), registry, store)["h2h_status"] == "ready"
    after = feature("current", market(), stamp(41), registry, store)
    assert after["h2h_status"] == "history_identity_invalidated"
    assert after["h2h_home_probability"] is None


def test_prior_and_current_sources_never_use_future_results(tmp_path):
    prior_session = tmp_path / "prior"
    build_session(prior_session, [(0, [match("old", 0)]), (40, [match("old", 40)])])
    raw, receipt = followup([later_result("known")])
    prior = prepare_prior(prior_session, raw, receipt)
    new_session = tmp_path / "new"
    times = iter(BASE + timedelta(seconds=s) for s in (110, 111))
    board = {"server_time": stamp(110), "matches": [match("new", 110), match("old", 110), match("known", 110)]}
    capture_one(new_session, 1, "odds", fetch=lambda _: json.dumps(board).encode(), clock=lambda: next(times))
    registry = ParticipantIdentityRegistry.initialize_pristine(tmp_path / "identity.json")
    store = OpponentIntelligenceStore.initialize_pristine(tmp_path / "opponents.json", registry)
    journal = build_journal(new_session, prior, registry, store, feature_cutoff=stamp(112))
    assert [r["fixture_id"] for r in journal["forecasts"]] == ["new"]
    assert journal["counts"]["excluded_prior_fixture_id"] == 1
    assert journal["counts"]["excluded_prior_known_result_id"] == 1
    assert journal["counts"]["h2h_identity_unresolved"] == 1
    assert journal["model_training_authorized"] is journal["real_money_execution"] is False
    with pytest.raises(ValueError, match="follow fresh"):
        build_journal(new_session, prior, registry, store, feature_cutoff=stamp(111))
    times = iter(BASE + timedelta(seconds=s) for s in (113, 114))
    capture_one(new_session, 1, "results", fetch=lambda _: b'{"sport":"table_tennis","results":[]}',
                clock=lambda: next(times))
    with pytest.raises(ValueError, match="no current results"):
        build_journal(new_session, prior, registry, store, feature_cutoff=stamp(115))


def test_forecast_envelope_binds_bytes_and_strict_order(tmp_path):
    session = tmp_path / "snapshots"
    build_session(session, [(0, [match("a", 0)]), (40, [match("a", 40)])])
    # This check concerns receipt chronology; use the first odds/result pair.
    (session / "0002.odds.body.json").unlink()
    (session / "0002.odds.receipt.json").unlink()
    (session / "0002.results.body.json").unlink()
    (session / "0002.results.receipt.json").unlink()
    odds_receipt = json.loads((session / "0001.odds.receipt.json").read_text())
    journal = {"feature_cutoff": stamp(1.25), "odds_sha256": odds_receipt["sha256"],
               "prior_result_sha256": SHA}
    raw = json.dumps(journal).encode()
    envelope = {"schema": "research.tt_forecast_receipt.v1", "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(), "odds_sha256": odds_receipt["sha256"],
                "prior_result_sha256": SHA, "computed_at": stamp(1.25), "frozen_at": stamp(1.5)}
    (tmp_path / "forecast.body.json").write_bytes(raw)
    (tmp_path / "forecast.receipt.json").write_text(json.dumps(envelope))
    assert verify_journal(tmp_path)[0] == journal
    invalid_clock = dict(envelope, frozen_at=stamp(2))
    (tmp_path / "forecast.receipt.json").write_text(json.dumps(invalid_clock))
    with pytest.raises(ValueError, match="chronology"):
        verify_journal(tmp_path)
    (tmp_path / "forecast.receipt.json").write_text(json.dumps(envelope))
    (tmp_path / "forecast.body.json").write_bytes(raw + b" ")
    with pytest.raises(ValueError, match="does not bind"):
        verify_journal(tmp_path)


def test_one_cycle_durably_freezes_before_requesting_results(tmp_path, monkeypatch):
    prior_session = tmp_path / "prior"
    build_session(prior_session, [(0, [match("old", 0)]), (40, [match("old", 40)])])
    raw, receipt = followup([later_result("known")])
    body_path, receipt_path = tmp_path / "prior.body.json", tmp_path / "prior.receipt.json"
    body_path.write_bytes(raw)
    receipt_path.write_text(json.dumps(receipt))
    root = tmp_path / "actual_run"
    ticks = iter(BASE + timedelta(seconds=s) for s in (112, 113))
    monkeypatch.setattr(causal_h2h_observer, "_utc_now", lambda: next(ticks))

    def simulated_capture(directory, seq, kind):
        if kind == "odds":
            payload = json.dumps({"server_time": stamp(110), "matches": [match("new", 110)]}).encode()
            times = iter(BASE + timedelta(seconds=s) for s in (110, 111))
        else:
            journal = (root / "forecast.body.json").read_bytes()
            envelope = json.loads((root / "forecast.receipt.json").read_text())
            assert envelope["sha256"] == hashlib.sha256(journal).hexdigest()
            assert envelope["frozen_at"] == stamp(113)
            payload = json.dumps({"sport": "table_tennis", "results": [{"fixture_id": "new"}]}).encode()
            times = iter(BASE + timedelta(seconds=s) for s in (114, 115))
        return capture_one(directory, seq, kind, fetch=lambda _: payload, clock=lambda: next(times))

    monkeypatch.setattr(causal_h2h_observer, "capture_one", simulated_capture)
    summary = run(prior_session, body_path, receipt_path, root)
    journal, envelope, _ = verify_journal(root)
    assert summary["counts"]["forecast_fixtures"] == 1
    assert summary["exact_ids_present_in_later_results"] == 1
    assert journal["forecasts"][0]["h2h_status"] == "identity_unresolved"
    assert "result" not in journal["forecasts"][0]
    assert envelope["frozen_at"] < summary["later_results_requested_at"]
