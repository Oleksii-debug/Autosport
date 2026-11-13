"""No provider access: all receipts, matches, identities and times are invented."""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal

import pytest

from autosport.participant_identity import (
    AliasRecord, EntityIdentity, EntityKind, EntityLineage, LineageRelation,
    ParticipantIdentityError, ParticipantIdentityRegistry,
)
from autosport.transparent_bandit_policy import BanditPolicyState
from causal_strength_h2h import compare
from record import instant
from synthetic_identity_merge_window import (
    START, WEEK, audit, example_receipts, run_demo, scenario_hash,
)
from synthetic_tt_learning_cycle import FIXTURE_SHA, _fixture_stores, _time


def check(receipts, at):
    return audit(receipts, expected_sha256=scenario_hash(receipts), decision_at=at)


def test_known_merge_is_visible_only_after_strict_boundary_and_survives_expiry():
    result = run_demo()
    same, after, later = (result[k] for k in
                          ("at_merge_receipt", "after_merge_receipt", "after_entry_disappears"))
    assert same["h2h_status"] == "ready" and same["h2h_support"] == 1
    assert after["h2h_status"] == later["h2h_status"] == "identity_lineage_unreconciled"
    assert after["strength_h2h_home_probability"] is later["strength_h2h_home_probability"] is None
    assert later["last_visible_receipt_mapping_count"] == 0
    assert later["persisted_distinct_mappings"] == later["current_subject_lineages"] == 1
    assert all(x["reopened_registry_matches"] for x in (same, after, later))
    assert all(x["live_adapter_or_runtime_gate"] is False for x in (same, after, later))


def test_identical_empty_polls_cannot_exclude_a_hidden_expired_merge():
    result = run_demo()
    missing = result["missed_entry"]
    assert result["hypothetical_hidden_merge_and_no_merge_have_same_empty_polls"] is True
    assert result["hidden_merge_world_is_invented_not_observed"] is True
    assert missing["gap_at_least_seven_days_can_hide_entry"] is True
    # "ready" is only the old comparator's view of recorded local evidence.
    assert missing["h2h_status"] == "ready" and missing["persisted_distinct_mappings"] == 0
    assert missing["polling_coverage_verified"] is missing["mapping_truth_verified"] is False
    assert missing["model_training_authorized"] is False


def test_unrelated_future_receipt_cannot_change_past_causal_cut():
    original = example_receipts()
    later = deepcopy(original)
    later[-1]["mappings"] = [{"old": "Z", "retained": "Z2"}]
    assert scenario_hash(original) != scenario_hash(later)
    assert check(original, _time(40)) == check(later, _time(40))
    assert check(original, _time(41)) == check(later, _time(41))


def test_repeated_observation_does_not_change_first_local_knowledge():
    original = example_receipts()
    repeated = deepcopy(original)
    repeated.insert(2, {"received_at": _time(60),
                        "mappings": [{"old": "A", "retained": "A2"}]})
    before_second = check(repeated, _time(60))
    after_second = check(repeated, _time(60.000001))
    assert before_second["h2h_status"] == after_second["h2h_status"] == "identity_lineage_unreconciled"
    assert after_second["visible_receipts"] == 3
    assert after_second["persisted_distinct_mappings"] == 1


def test_lineage_binds_first_invented_receipt_and_does_not_mutate_input(monkeypatch):
    receipts = example_receipts()
    receipts.insert(2, {"received_at": _time(60),
                        "mappings": [{"old": "A", "retained": "A2"}]})
    original_receipts = deepcopy(receipts)
    captured = []
    original_add = ParticipantIdentityRegistry.add_lineage

    def observe(registry, lineage):
        captured.append(lineage)
        return original_add(registry, lineage)

    monkeypatch.setattr(ParticipantIdentityRegistry, "add_lineage", observe)
    check(receipts, _time(61))
    # Reopening replays the one saved lineage through the canonical loader.
    assert len(captured) == 2
    for record in captured:
        assert instant(record.available_at) == instant(record.recorded_at) == instant(_time(40))
        assert instant(record.effective_from) == instant(_time(40))
        assert record.evidence_sha256 == scenario_hash(receipts[1])
    assert receipts == original_receipts


def test_blocked_feature_does_not_intercept_a_direct_policy_call():
    result = check(example_receipts(), _time(41))
    assert result["strength_h2h_home_probability"] is None
    policy = BanditPolicyState.initial(environment_id="b" * 64, protocol_id="synthetic",
                                       config_sha256="a" * 64, seed=0,
                                       action_types=frozenset({"MARKET", "FORM_H2H"}))
    assert policy.choose(admissible_actions=frozenset({"FORM_H2H"})) == "FORM_H2H"
    assert result["live_adapter_or_runtime_gate"] is False


def test_unrelated_merge_does_not_rewrite_current_identity_or_h2h():
    receipts = [{"received_at": _time(40), "mappings": [{"old": "Z", "retained": "Z2"}]}]
    result = check(receipts, _time(41))
    assert result["persisted_distinct_mappings"] == 1
    assert result["current_subject_lineages"] == 0 and result["h2h_status"] == "ready"


def test_seven_day_gap_boundary_and_short_gap_is_not_proof():
    early = {"received_at": _time(20), "mappings": []}
    late = {"received_at": (START + timedelta(seconds=20) + WEEK).isoformat(), "mappings": []}
    exact = check([early, late], (START + timedelta(seconds=21) + WEEK).isoformat())
    assert exact["gap_at_least_seven_days_can_hide_entry"] is True
    late["received_at"] = (START + timedelta(seconds=20) + WEEK - timedelta(microseconds=1)).isoformat()
    short = check([early, late], (START + timedelta(seconds=21) + WEEK - timedelta(microseconds=1)).isoformat())
    assert short["gap_at_least_seven_days_can_hide_entry"] is False
    assert short["polling_coverage_verified"] is False


def test_source_competitor_pair_cannot_be_assumed_single_participant(tmp_path):
    registry, store = _fixture_stores(tmp_path)
    registry.add_entity(EntityIdentity("pair", EntityKind.TEAM, "invented:pair", FIXTURE_SHA,
                                       _time(0), _time(0)))
    registry.add_alias(AliasRecord("moviodds.com", "pair", "pair", _time(0), None,
                                   _time(0), FIXTURE_SHA, _time(0)))
    market = ("pair", "B", "L", {"pair": ("1", Decimal("1.5")), "B": ("2", Decimal("2.5"))})
    result = compare("invented-current", market, _time(31), registry, store)
    assert result["h2h_status"] == "identity_incompatible"
    assert result["strength_h2h_home_probability"] is None
    with pytest.raises(ParticipantIdentityError, match="same EntityKind"):
        registry.add_lineage(EntityLineage("A", "pair", LineageRelation.MERGED_FROM,
                                           _time(30), _time(30), _time(30), FIXTURE_SHA))


@pytest.mark.parametrize("change, message", [
    ("duplicate", "duplicate source"), ("conflict", "conflicting"),
    ("cycle", "cyclic"), ("same", "distinct fixture"),
    ("unknown", "distinct fixture"), ("order", "increase"),
    ("naive_time", "receipt time"), ("extra_field", "receipt fields"),
])
def test_bad_synthetic_receipts_fail_closed(change, message):
    receipts = example_receipts()
    if change == "duplicate":
        receipts[1]["mappings"].append(deepcopy(receipts[1]["mappings"][0]))
    elif change == "conflict":
        receipts[-1]["mappings"] = [{"old": "A", "retained": "B"}]
    elif change == "cycle":
        receipts[-1]["mappings"] = [{"old": "A2", "retained": "A"}]
    elif change == "same":
        receipts[1]["mappings"] = [{"old": "A", "retained": "A"}]
    elif change == "unknown":
        receipts[1]["mappings"] = [{"old": "A", "retained": "some-real-id"}]
    elif change == "order":
        receipts[1]["received_at"] = receipts[0]["received_at"]
    elif change == "naive_time":
        receipts[0]["received_at"] = "2026-01-01T00:00:20"
    else:
        receipts[0]["extra"] = "invented"
    with pytest.raises(ValueError, match=message):
        check(receipts, _time(9 * 24 * 60 * 60))


def test_external_pin_and_empty_receipts_do_not_prove_absence():
    receipts = example_receipts()
    with pytest.raises(ValueError, match="digest"):
        audit(receipts, expected_sha256="f" * 64, decision_at=_time(41))
    empty = check([], _time(8 * 24 * 60 * 60))
    assert empty["visible_receipts"] == 0 and empty["last_visible_receipt_mapping_count"] is None
    assert empty["gap_at_least_seven_days_can_hide_entry"] is True
    assert empty["polling_coverage_verified"] is False
