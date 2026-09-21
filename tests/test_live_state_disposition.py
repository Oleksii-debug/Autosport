from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from autosport.live_state_disposition import (
    LiveDispositionRecord,
    LiveStateDisposition,
    LiveStateDispositionError,
    LiveStateEvidence,
    UK_UA_DISPOSITION_LABELS,
    decide_live_state_disposition,
)


NOW = datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc)
TTL = timedelta(minutes=2)


def _evidence(**overrides: object) -> LiveStateEvidence:
    values: dict[str, object] = {
        "decision_cycle_id": "cycle-001",
        "canonical_input_bundle_id": "bundle-001",
        "evidence_version": "evidence-v1",
        "policy_version": "policy-v1",
        "state_observed_at": NOW,
        "freshness_cutoff_at": NOW - timedelta(seconds=5),
        "evidence_complete": True,
    }
    values.update(overrides)
    return LiveStateEvidence(**values)  # type: ignore[arg-type]


def test_ambiguous_evidence_waits_and_never_allows_submit() -> None:
    record = decide_live_state_disposition(
        _evidence(evidence_complete=False), evaluated_at=NOW, wait_ttl=TTL
    )

    assert record is not None
    assert record.disposition is LiveStateDisposition.WAIT_EVIDENCE
    assert record.reason_code == "LIVE_EVIDENCE_INCOMPLETE_OR_STALE"
    assert record.waiting_expires_at == NOW + TTL
    assert record.operator_required is False
    assert record.auto_submit_allowed is False


def test_same_identity_wait_is_deterministic_and_preserves_original_deadline() -> None:
    evidence = _evidence(evidence_complete=False)
    first = decide_live_state_disposition(evidence, evaluated_at=NOW, wait_ttl=TTL)
    assert first is not None

    second = decide_live_state_disposition(
        evidence,
        evaluated_at=NOW + timedelta(seconds=30),
        wait_ttl=timedelta(minutes=30),
        prior=first,
    )

    assert second is not None
    assert second.disposition is LiveStateDisposition.WAIT_EVIDENCE
    assert second.first_seen_at == NOW
    assert second.waiting_expires_at == NOW + TTL
    assert second.previous_disposition is LiveStateDisposition.WAIT_EVIDENCE


def test_new_evidence_identity_can_resolve_wait_without_granting_execution_authority() -> None:
    waiting = decide_live_state_disposition(
        _evidence(evidence_complete=False), evaluated_at=NOW, wait_ttl=TTL
    )
    assert waiting is not None

    resolved = decide_live_state_disposition(
        _evidence(evidence_version="evidence-v2"),
        evaluated_at=NOW + timedelta(seconds=30),
        wait_ttl=TTL,
        prior=waiting,
    )

    assert resolved is None


def test_wait_ttl_expiry_escalates_and_restart_cannot_reset_clock() -> None:
    evidence = _evidence(evidence_complete=False)
    waiting = decide_live_state_disposition(evidence, evaluated_at=NOW, wait_ttl=TTL)
    assert waiting is not None
    reloaded = LiveDispositionRecord.from_dict(waiting.to_dict())

    escalated = decide_live_state_disposition(
        evidence,
        evaluated_at=NOW + TTL,
        wait_ttl=timedelta(hours=1),
        prior=reloaded,
        restart_reloaded=True,
    )

    assert escalated is not None
    assert escalated.disposition is LiveStateDisposition.ESCALATE_OPERATOR
    assert escalated.reason_code == "WAIT_EVIDENCE_EXPIRED"
    assert escalated.first_seen_at == NOW
    assert escalated.waiting_expires_at == NOW + TTL
    assert escalated.operator_required is True
    assert escalated.restart_reloaded is True
    assert escalated.auto_submit_allowed is False


def test_restart_before_ttl_round_trips_identity_and_deadline() -> None:
    evidence = _evidence(evidence_complete=False)
    waiting = decide_live_state_disposition(evidence, evaluated_at=NOW, wait_ttl=TTL)
    assert waiting is not None

    reloaded = LiveDispositionRecord.from_dict(waiting.to_dict())
    again = decide_live_state_disposition(
        evidence,
        evaluated_at=NOW + timedelta(seconds=45),
        wait_ttl=timedelta(days=1),
        prior=reloaded,
        restart_reloaded=True,
    )

    assert reloaded == waiting
    assert again is not None
    assert again.identity == waiting.identity
    assert again.waiting_expires_at == waiting.waiting_expires_at
    assert again.first_seen_at == waiting.first_seen_at
    assert again.restart_reloaded is True


def test_contradiction_and_unsafe_restart_fail_closed() -> None:
    contradiction = decide_live_state_disposition(
        _evidence(contradictory=True), evaluated_at=NOW, wait_ttl=TTL
    )
    unsafe_restart = decide_live_state_disposition(
        _evidence(unsafe_restart_state=True), evaluated_at=NOW, wait_ttl=TTL
    )
    conflicting_terminal = decide_live_state_disposition(
        _evidence(policy_declined=True, infeasible=True), evaluated_at=NOW, wait_ttl=TTL
    )

    assert contradiction is not None
    assert contradiction.disposition is LiveStateDisposition.SAFE_ABORT
    assert contradiction.reason_code == "EVIDENCE_CONTRADICTION"
    assert unsafe_restart is not None
    assert unsafe_restart.disposition is LiveStateDisposition.SAFE_ABORT
    assert unsafe_restart.reason_code == "UNSAFE_RESTART_STATE"
    assert conflicting_terminal is not None
    assert conflicting_terminal.disposition is LiveStateDisposition.SAFE_ABORT
    assert conflicting_terminal.reason_code == "CONFLICTING_TERMINAL_ASSESSMENTS"
    assert all(
        record.operator_required and not record.auto_submit_allowed
        for record in (contradiction, unsafe_restart, conflicting_terminal)
    )


def test_fresh_complete_policy_decline_and_infeasible_are_distinct_terminal_outcomes() -> None:
    policy = decide_live_state_disposition(
        _evidence(policy_declined=True), evaluated_at=NOW, wait_ttl=TTL
    )
    infeasible = decide_live_state_disposition(
        _evidence(infeasible=True), evaluated_at=NOW, wait_ttl=TTL
    )

    assert policy is not None
    assert policy.disposition is LiveStateDisposition.NO_BET_POLICY
    assert policy.reason_code == "POLICY_DECLINED"
    assert infeasible is not None
    assert infeasible.disposition is LiveStateDisposition.NO_BET_INFEASIBLE
    assert infeasible.reason_code == "MARKET_INFEASIBLE"
    assert policy != infeasible
    assert not policy.operator_required
    assert not infeasible.operator_required


def test_no_bet_is_terminal_for_same_identity_but_new_policy_version_reopens_evaluation() -> None:
    evidence = _evidence(policy_declined=True)
    declined = decide_live_state_disposition(evidence, evaluated_at=NOW, wait_ttl=TTL)
    assert declined is not None

    same_identity = decide_live_state_disposition(
        replace(evidence, policy_declined=False),
        evaluated_at=NOW + timedelta(seconds=10),
        wait_ttl=TTL,
        prior=declined,
    )
    new_policy = decide_live_state_disposition(
        replace(evidence, policy_version="policy-v2", policy_declined=False),
        evaluated_at=NOW + timedelta(seconds=10),
        wait_ttl=TTL,
        prior=declined,
    )

    assert same_identity is not None
    assert same_identity.disposition is LiveStateDisposition.NO_BET_POLICY
    assert new_policy is None


def test_escalation_requires_token_or_new_evidence_identity_for_reconsideration() -> None:
    evidence = _evidence(evidence_complete=False)
    waiting = decide_live_state_disposition(evidence, evaluated_at=NOW, wait_ttl=TTL)
    assert waiting is not None
    escalated = decide_live_state_disposition(
        evidence, evaluated_at=NOW + TTL, wait_ttl=TTL, prior=waiting
    )
    assert escalated is not None

    still_escalated = decide_live_state_disposition(
        replace(evidence, evidence_complete=True),
        evaluated_at=NOW + TTL + timedelta(seconds=1),
        wait_ttl=TTL,
        prior=escalated,
    )
    operator_rechecked = decide_live_state_disposition(
        replace(evidence, evidence_complete=True),
        evaluated_at=NOW + TTL + timedelta(seconds=1),
        wait_ttl=TTL,
        prior=escalated,
        operator_resolution_token="operator-resolution-001",
    )

    assert still_escalated is not None
    assert still_escalated.disposition is LiveStateDisposition.ESCALATE_OPERATOR
    assert operator_rechecked is None


@pytest.mark.parametrize(
    ("changes", "expected"),
    (
        ({"evidence_complete": False}, LiveStateDisposition.WAIT_EVIDENCE),
        ({"policy_declined": True}, LiveStateDisposition.NO_BET_POLICY),
        ({"infeasible": True}, LiveStateDisposition.NO_BET_INFEASIBLE),
        ({"contradictory": True}, LiveStateDisposition.SAFE_ABORT),
    ),
)
def test_every_non_action_outcome_is_fail_closed(
    changes: dict[str, object], expected: LiveStateDisposition
) -> None:
    record = decide_live_state_disposition(_evidence(**changes), evaluated_at=NOW, wait_ttl=TTL)
    assert record is not None
    assert record.disposition is expected
    assert record.auto_submit_allowed is False


def test_persisted_payload_rejects_auto_submit_escalation_and_unknown_fields() -> None:
    waiting = decide_live_state_disposition(
        _evidence(evidence_complete=False), evaluated_at=NOW, wait_ttl=TTL
    )
    assert waiting is not None
    payload = waiting.to_dict()
    payload["auto_submit_allowed"] = True
    with pytest.raises(LiveStateDispositionError, match="auto-submit"):
        LiveDispositionRecord.from_dict(payload)

    payload = waiting.to_dict()
    payload["unexpected"] = "drift"
    with pytest.raises(LiveStateDispositionError, match="fields mismatch"):
        LiveDispositionRecord.from_dict(payload)


def test_invalid_timestamps_ttl_and_polymorphic_authority_inputs_fail_closed() -> None:
    with pytest.raises(LiveStateDispositionError, match="timezone-aware"):
        _evidence(state_observed_at=NOW.replace(tzinfo=None))
    with pytest.raises(LiveStateDispositionError, match="positive timedelta"):
        decide_live_state_disposition(_evidence(), evaluated_at=NOW, wait_ttl=timedelta(0))

    base = _evidence()
    forged_type = type("ForgedLiveStateEvidence", (LiveStateEvidence,), {})
    forged = forged_type(
        decision_cycle_id=base.decision_cycle_id,
        canonical_input_bundle_id=base.canonical_input_bundle_id,
        evidence_version=base.evidence_version,
        policy_version=base.policy_version,
        state_observed_at=base.state_observed_at,
        freshness_cutoff_at=base.freshness_cutoff_at,
        evidence_complete=base.evidence_complete,
    )
    with pytest.raises(TypeError, match="exact LiveStateEvidence"):
        decide_live_state_disposition(forged, evaluated_at=NOW, wait_ttl=TTL)


def test_ukrainian_labels_cover_every_disposition_without_claiming_nvda_verification() -> None:
    assert set(UK_UA_DISPOSITION_LABELS) == set(LiveStateDisposition)
    assert UK_UA_DISPOSITION_LABELS[LiveStateDisposition.WAIT_EVIDENCE] == "Очікуємо нові ринкові дані"
    assert UK_UA_DISPOSITION_LABELS[LiveStateDisposition.SAFE_ABORT] == "Автоматичне виконання зупинено"
