"""All identities, observations, and outcomes in these tests are invented."""
from dataclasses import replace

import pytest

from autosport.learning_environment import EvidenceTruth
from autosport.participant_identity import (
    EntityIdentity, EntityKind, EntityLineage, LineageRelation, ParticipantIdentityRegistry,
)
from synthetic_learning_identity_audit import SimulatedIdentityReceipt, audit, timeline_head
from test_causal_strength_frozen import dated, seconds
from test_causal_strength_revision_history import add
from test_synthetic_learning_revision_audit import check, prepare


def receipt(at, snapshot, truth=EvidenceTruth.SIMULATED):
    return SimulatedIdentityReceipt(seconds(at).isoformat(), snapshot, truth)


def changed_registry(root, *, unrelated=False, available_at=300):
    registry = ParticipantIdentityRegistry(root / "identities.json")
    if unrelated:
        registry.add_entity(EntityIdentity("unrelated", EntityKind.PARTICIPANT,
                                           "invented:unrelated", "c" * 64,
                                           seconds(1).isoformat(), seconds(1).isoformat()))
    else:
        registry.add_lineage(EntityLineage("A", "G", LineageRelation.MERGED_FROM,
                                            seconds(50).isoformat(), seconds(available_at).isoformat(),
                                            seconds(available_at).isoformat(), "b" * 64))
    return (root / "identities.json").read_bytes()


def run(root, history, result_head, pins, policy, bindings, frozen, receipts, *,
        when=250, identity_head=None):
    if identity_head is None:
        identity_head = timeline_head(pins["expected_forecast_sha256"], frozen, receipts)
    return audit(root, history, result_head, **pins, frozen_identity=frozen,
                 identity_receipts=receipts, expected_identity_head=identity_head,
                 policy=policy, bindings=bindings, decision_at=seconds(when).isoformat())


def test_identity_merge_holds_only_after_receipt_and_reversion_does_not_clear(tmp_path):
    history, pins, head, _, policy, bindings = prepare(tmp_path)
    frozen = (tmp_path / "identities.json").read_bytes()
    changed = changed_registry(tmp_path)
    records = (receipt(230, frozen), receipt(300, changed), receipt(400, frozen))
    original_policy = policy
    # The existing result-only audit cannot see this identity change.
    assert check(tmp_path, history, head, pins, policy, bindings, 401)["status"] == "ready_for_simulation"
    first = run(tmp_path, history, head, pins, policy, bindings, frozen, records, when=250)
    equal = run(tmp_path, history, head, pins, policy, bindings, frozen, records, when=300)
    after = run(tmp_path, history, head, pins, policy, bindings, frozen, records, when=301)
    reverted = run(tmp_path, history, head, pins, policy, bindings, frozen, records, when=401)
    assert first["status"] == equal["status"] == "no_observed_change_only"
    assert after["status"] == reverted["status"] == "held_for_review"
    assert after["identity_status"] == reverted["identity_status"] == "identity_changed_hold"
    assert [first["visible_identity_receipts"], equal["visible_identity_receipts"],
            after["visible_identity_receipts"], reverted["visible_identity_receipts"]] == [1, 1, 2, 3]
    assert reverted["visible_changed_identity_receipts"] == 1
    assert reverted["verified_policy_generation"] == reverted["verified_simulated_bindings"] == 1
    assert policy == original_policy and reverted["policy_modified"] is False
    assert reverted["production_enforcement"] is reverted["real_money_execution"] is False
    assert reverted["identity_continuity_verified"] is reverted["identity_independently_verified"] is False


def test_missing_snapshot_holds_even_with_a_valid_empty_chain(tmp_path):
    history, pins, head, _, policy, bindings = prepare(tmp_path)
    frozen = (tmp_path / "identities.json").read_bytes()
    result = run(tmp_path, history, head, pins, policy, bindings, frozen, [])
    assert result["status"] == "held_for_review"
    assert result["identity_status"] == "identity_snapshot_missing_hold"
    future = run(tmp_path, history, head, pins, policy, bindings, frozen, [receipt(300, frozen)])
    assert future["identity_status"] == "identity_snapshot_missing_hold"


@pytest.mark.parametrize("change", ["unrelated", "formatting"])
def test_byte_change_is_conservatively_quarantined_even_if_unrelated(tmp_path, change):
    history, pins, head, _, policy, bindings = prepare(tmp_path)
    frozen = (tmp_path / "identities.json").read_bytes()
    changed = changed_registry(tmp_path, unrelated=True) if change == "unrelated" else frozen + b" "
    result = run(tmp_path, history, head, pins, policy, bindings, frozen, [receipt(230, changed)])
    assert result["status"] == "held_for_review"
    assert result["bytewise_comparison_can_overquarantine"] is True


def test_update_after_known_identity_change_is_rejected_but_equal_time_is_excluded(tmp_path):
    history, pins, head, _, policy, bindings = prepare(tmp_path)
    frozen = (tmp_path / "identities.json").read_bytes()
    changed = changed_registry(tmp_path, available_at=200)
    with pytest.raises(ValueError, match="update followed a known identity"):
        run(tmp_path, history, head, pins, policy, bindings, frozen, [receipt(200, changed)])
    equal = run(tmp_path, history, head, pins, policy, bindings, frozen, [receipt(201, changed)])
    assert equal["status"] == "held_for_review"


def test_future_change_does_not_change_past_decision_fields(tmp_path):
    history, pins, head, _, policy, bindings = prepare(tmp_path)
    frozen = (tmp_path / "identities.json").read_bytes()
    changed = changed_registry(tmp_path)
    past = run(tmp_path, history, head, pins, policy, bindings, frozen, [receipt(230, frozen)])
    future = run(tmp_path, history, head, pins, policy, bindings, frozen,
                 [receipt(230, frozen), receipt(300, changed)])
    assert {k: v for k, v in past.items() if k != "identity_chain_head"} == {
        k: v for k, v in future.items() if k != "identity_chain_head"}
    assert past["identity_chain_head"] != future["identity_chain_head"]


def test_prior_result_correction_also_holds_policy(tmp_path):
    history, pins, head, original, policy, bindings = prepare(tmp_path)
    frozen = (tmp_path / "identities.json").read_bytes()
    head = add(tmp_path, history, pins, head,
               [dated("full", "A", "B", "away"), original[1], original[2]], 1)
    report = run(tmp_path, history, head, pins, policy, bindings, frozen,
                 [receipt(230, frozen)], when=301)
    assert report["identity_status"] == "no_identity_change_observed"
    assert report["result_revision_status"] == report["status"] == "held_for_review"


def test_reordered_removed_or_changed_receipts_fail_external_pin(tmp_path):
    history, pins, head, _, policy, bindings = prepare(tmp_path)
    frozen = (tmp_path / "identities.json").read_bytes()
    changed = changed_registry(tmp_path)
    records = [receipt(230, frozen), receipt(300, changed)]
    pin = timeline_head(pins["expected_forecast_sha256"], frozen, records)
    for tampered in (records[:1], [receipt(230, frozen), receipt(300, frozen)],
                     [receipt(230, frozen), receipt(301, changed)]):
        with pytest.raises(ValueError, match="external pin"):
            run(tmp_path, history, head, pins, policy, bindings, frozen, tampered, identity_head=pin)
    with pytest.raises(ValueError, match="strictly increase"):
        run(tmp_path, history, head, pins, policy, bindings, frozen, records[::-1], identity_head=pin)


def test_frozen_snapshot_cannot_be_rebound_even_with_recomputed_identity_pin(tmp_path):
    history, pins, head, _, policy, bindings = prepare(tmp_path)
    frozen = (tmp_path / "identities.json").read_bytes()
    forged_frozen = frozen + b" "
    forged = [receipt(230, forged_frozen)]
    forged_pin = timeline_head(pins["expected_forecast_sha256"], forged_frozen, forged)
    with pytest.raises(ValueError, match="forecast-bound digest"):
        run(tmp_path, history, head, pins, policy, bindings, forged_frozen, forged,
            identity_head=forged_pin)


def test_caller_supplied_stale_pin_cannot_prove_history_is_complete(tmp_path):
    history, pins, head, _, policy, bindings = prepare(tmp_path)
    frozen = (tmp_path / "identities.json").read_bytes()
    changed = changed_registry(tmp_path)
    latest = [receipt(230, frozen), receipt(300, changed)]
    full = run(tmp_path, history, head, pins, policy, bindings, frozen, latest, when=301)
    # A self-consistent but obsolete caller pin provides no freshness guarantee.
    stale = run(tmp_path, history, head, pins, policy, bindings, frozen, latest[:1], when=301)
    assert full["status"] == "held_for_review"
    assert stale["status"] == "no_observed_change_only"
    assert stale["pin_freshness_verified"] is stale["identity_source_complete"] is False


def test_only_valid_chronological_simulated_snapshots_are_accepted(tmp_path):
    history, pins, head, _, policy, bindings = prepare(tmp_path)
    frozen = (tmp_path / "identities.json").read_bytes()
    forecast = pins["expected_forecast_sha256"]
    invalid_chains = [
        [receipt(230, frozen, EvidenceTruth.OBSERVED)],
        [receipt(230, b"{}")],
        [receipt(230, b"\xff")],
        [receipt(230, frozen), receipt(230, frozen)],
        [replace(receipt(230, frozen), received_at="2026-10-29T00:03:50")],
        [receipt(230, frozen)] * 129,
    ]
    for invalid in invalid_chains:
        with pytest.raises(ValueError):
            timeline_head(forecast, frozen, invalid)
    with pytest.raises(ValueError, match="strictly follow forecast"):
        run(tmp_path, history, head, pins, policy, bindings, frozen, [receipt(112, frozen)])
    with pytest.raises(ValueError, match="does not cover"):
        run(tmp_path, history, head, pins, policy, [], frozen, [receipt(230, frozen)])


def test_audit_does_not_intercept_direct_policy_calls(tmp_path):
    history, pins, head, _, policy, bindings = prepare(tmp_path)
    frozen = (tmp_path / "identities.json").read_bytes()
    report = run(tmp_path, history, head, pins, policy, bindings, frozen, [])
    assert report["status"] == "held_for_review"
    allowed = frozenset(estimate.action_type for estimate in policy.estimates)
    assert policy.choose(admissible_actions=allowed) in allowed
