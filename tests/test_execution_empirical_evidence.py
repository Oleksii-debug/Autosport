from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from autosport.execution_empirical_evidence import (
    EmpiricalExecutionEvidenceError,
    EmpiricalExecutionEvidenceUnavailable,
    SLIPPAGE_STATUS_KNOWN,
    SLIPPAGE_STATUS_NOT_APPLICABLE,
    SLIPPAGE_STATUS_UNKNOWN,
    TIMING_REASON_NO_MONOTONIC_WITNESS,
    TIMING_STATUS_UNKNOWN,
    build_empirical_execution_evidence,
)
from autosport.real_execution_ledger import (
    AcknowledgementStatus,
    ExecutionAction,
    ExecutionLedgerIntegrityError,
    ExecutionPlan,
    ExternalAcknowledgement,
    RealExecutionLedger,
    ReconciliationSnapshot,
)

QUOTE = "2026-09-21T10:00:00+00:00"
DECISION = "2026-09-21T10:00:01+00:00"
RESERVED = "2026-09-21T10:00:01.100000+00:00"
SUBMITTED = "2026-09-21T10:00:01.250000+00:00"
PROVIDER = "2026-09-21T10:00:01.700000+00:00"
ACKED = "2026-09-21T10:00:01.800000+00:00"
EVIDENCE_ID = "e" * 64


def _action(**changes: object) -> ExecutionAction:
    action = ExecutionAction(
        action_id="leg-1",
        bookmaker_id="provider-1",
        account_id="account-1",
        event_id="event-1",
        market_id="market-1",
        selection_id="selection-1",
        side="BACK",
        requested_odds=Decimal("2.10"),
        requested_stake=Decimal("5.00"),
        quote_id="quote-1",
        quote_observed_at=QUOTE,
        expires_at="2026-09-21T10:01:00+00:00",
    )
    return replace(action, **changes) if changes else action


def _reserved_ledger(
    tmp_path,
    *,
    action: ExecutionAction | None = None,
) -> RealExecutionLedger:
    ledger = RealExecutionLedger(tmp_path / "execution.jsonl")
    plan = ExecutionPlan(
        plan_id="plan-1",
        bookmaker_profile_version="profile-1",
        decision_id="decision-1",
        approval_id="approval-1",
        created_at=DECISION,
        actions=(action or _action(),),
    )
    ledger.reserve_plan(plan)
    ledger.begin_attempt(
        plan_id=plan.plan_id,
        action_id=plan.actions[0].action_id,
        attempt_id="attempt-1",
        reserved_at=RESERVED,
    )
    return ledger


def _ledger(
    tmp_path,
    *,
    action: ExecutionAction | None = None,
) -> RealExecutionLedger:
    ledger = _reserved_ledger(tmp_path, action=action)
    ledger.mark_submitted("attempt-1", submitted_at=SUBMITTED)
    return ledger


def _bind_provider(
    ledger: RealExecutionLedger,
    *,
    observed_at: str = PROVIDER,
) -> None:
    ledger.bind_provider_evidence(
        attempt_id="attempt-1",
        evidence_id=EVIDENCE_ID,
        observed_at=observed_at,
        source="provider-response",
    )


def _ack(
    ledger: RealExecutionLedger,
    *,
    status: AcknowledgementStatus = AcknowledgementStatus.ACCEPTED,
    accepted_odds: Decimal | None = Decimal("2.08"),
    accepted_stake: Decimal | None = Decimal("5.00"),
    acknowledged_at: str = ACKED,
) -> None:
    ledger.acknowledge(
        ExternalAcknowledgement(
            attempt_id="attempt-1",
            external_receipt_id="receipt-1",
            status=status,
            acknowledged_at=acknowledged_at,
            accepted_odds=accepted_odds,
            accepted_stake=accepted_stake,
        )
    )


def _accepted_evidence(tmp_path):
    ledger = _ledger(tmp_path)
    _bind_provider(ledger)
    _ack(ledger)
    return build_empirical_execution_evidence(
        ledger,
        attempt_id="attempt-1",
    )


def test_terminal_accepted_record_keeps_slippage_but_latency_unknown(tmp_path):
    evidence = _accepted_evidence(tmp_path)

    assert evidence.attempt_state == "ACCEPTED"
    assert evidence.terminal is True
    assert evidence.right_censored is False
    assert evidence.censor_reason is None
    assert evidence.censor_cutoff_recorded_at is None

    assert evidence.slippage_status == SLIPPAGE_STATUS_KNOWN
    assert evidence.accepted_minus_requested_odds == Decimal("-0.02")
    assert evidence.adverse_odds_delta == Decimal("0.02")
    assert evidence.unaccepted_stake == Decimal("0.00")

    assert evidence.causal_timing_status == TIMING_STATUS_UNKNOWN
    assert evidence.causal_timing_reason == TIMING_REASON_NO_MONOTONIC_WITNESS
    assert evidence.quote_age_at_decision_us is None
    assert evidence.decision_to_reserve_us is None
    assert evidence.decision_to_submit_us is None
    assert evidence.submit_to_provider_evidence_us is None
    assert evidence.submit_to_acknowledgement_us is None
    assert evidence.decision_to_acknowledgement_us is None


def test_submitted_attempt_is_retained_as_right_censored_evidence(tmp_path):
    ledger = _ledger(tmp_path)

    evidence = build_empirical_execution_evidence(
        ledger,
        attempt_id="attempt-1",
    )

    assert evidence.attempt_state == "SUBMITTED"
    assert evidence.terminal is False
    assert evidence.right_censored is True
    assert evidence.censor_reason == "SUBMITTED_NO_TERMINAL_ACK"
    assert evidence.censor_cutoff_event_count == evidence.source_event_count
    assert evidence.censor_cutoff_recorded_at is not None
    assert evidence.acknowledgement_status is None
    assert evidence.slippage_status == SLIPPAGE_STATUS_UNKNOWN
    assert evidence.accepted_odds is None


def test_reserved_attempt_is_retained_in_population(tmp_path):
    ledger = _reserved_ledger(tmp_path)

    evidence = build_empirical_execution_evidence(
        ledger,
        attempt_id="attempt-1",
    )

    assert evidence.attempt_state == "RESERVED"
    assert evidence.submitted_at is None
    assert evidence.right_censored is True
    assert evidence.censor_reason == "RESERVED_NOT_SUBMITTED"
    assert evidence.slippage_status == SLIPPAGE_STATUS_UNKNOWN


def test_unknown_attempt_is_retained_with_explicit_censor_reason(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.mark_unknown(
        "attempt-1",
        reason="provider timeout after submission",
        observed_at="2026-09-21T10:00:02+00:00",
    )

    evidence = build_empirical_execution_evidence(
        ledger,
        attempt_id="attempt-1",
    )

    assert evidence.attempt_state == "UNKNOWN"
    assert evidence.right_censored is True
    assert evidence.censor_reason == "UNKNOWN_EXTERNAL_EFFECT"
    assert evidence.slippage_status == SLIPPAGE_STATUS_UNKNOWN
    assert evidence.acknowledged_at is None


def test_provider_evidence_does_not_turn_nonterminal_attempt_into_slippage_sample(
    tmp_path,
):
    ledger = _ledger(tmp_path)
    _bind_provider(ledger)

    evidence = build_empirical_execution_evidence(
        ledger,
        attempt_id="attempt-1",
    )

    assert evidence.attempt_state == "SUBMITTED"
    assert evidence.provider_evidence_id == EVIDENCE_ID
    assert evidence.provider_evidence_observed_at == PROVIDER
    assert evidence.slippage_status == SLIPPAGE_STATUS_UNKNOWN
    assert evidence.accepted_odds is None


def test_terminal_ack_without_separate_provider_evidence_still_has_attempt_record(
    tmp_path,
):
    ledger = _ledger(tmp_path)
    _ack(ledger)

    evidence = build_empirical_execution_evidence(
        ledger,
        attempt_id="attempt-1",
    )

    assert evidence.attempt_state == "ACCEPTED"
    assert evidence.provider_evidence_id is None
    assert evidence.slippage_status == SLIPPAGE_STATUS_UNKNOWN
    assert evidence.accepted_odds is None
    assert evidence.adverse_odds_delta is None


def test_reconciled_not_found_attempt_remains_right_censored(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.mark_unknown(
        "attempt-1",
        reason="provider timeout after submission",
        observed_at="2026-09-21T10:00:02+00:00",
    )
    ledger.reconcile_not_found(
        ReconciliationSnapshot(
            attempt_id="attempt-1",
            evidence_id="r" * 64,
            observed_at="2026-09-21T10:00:03+00:00",
            external_effect_found=False,
            source="provider-order-readback",
        )
    )

    evidence = build_empirical_execution_evidence(
        ledger,
        attempt_id="attempt-1",
    )

    assert evidence.attempt_state == "RECONCILED_NOT_FOUND"
    assert evidence.right_censored is True
    assert evidence.censor_reason == "RECONCILED_NOT_FOUND_NO_TERMINAL_ACK"
    assert evidence.slippage_status == SLIPPAGE_STATUS_UNKNOWN


def test_direct_construction_cannot_mint_known_slippage_without_provider_evidence(
    tmp_path,
):
    ledger = _ledger(tmp_path)
    _ack(ledger)
    evidence = build_empirical_execution_evidence(
        ledger,
        attempt_id="attempt-1",
    )

    with pytest.raises(
        EmpiricalExecutionEvidenceError,
        match="KNOWN slippage requires bound provider evidence",
    ):
        replace(
            evidence,
            slippage_status=SLIPPAGE_STATUS_KNOWN,
            accepted_odds=Decimal("2.08"),
            accepted_stake=Decimal("5.00"),
            accepted_minus_requested_odds=Decimal("-0.02"),
            adverse_odds_delta=Decimal("0.02"),
            unaccepted_stake=Decimal("0.00"),
        )


def test_partial_acceptance_preserves_exact_unaccepted_stake(tmp_path):
    ledger = _ledger(tmp_path)
    _bind_provider(ledger)
    _ack(
        ledger,
        status=AcknowledgementStatus.PARTIAL,
        accepted_odds=Decimal("2.10"),
        accepted_stake=Decimal("3.25"),
    )

    evidence = build_empirical_execution_evidence(
        ledger,
        attempt_id="attempt-1",
    )

    assert evidence.attempt_state == "PARTIAL"
    assert evidence.acknowledgement_status == "PARTIAL"
    assert evidence.slippage_status == SLIPPAGE_STATUS_KNOWN
    assert evidence.unaccepted_stake == Decimal("1.75")
    assert evidence.adverse_odds_delta == Decimal("0")


def test_rejected_attempt_is_not_a_fake_zero_slippage_sample(tmp_path):
    ledger = _ledger(tmp_path)
    _bind_provider(ledger)
    _ack(
        ledger,
        status=AcknowledgementStatus.REJECTED,
        accepted_odds=None,
        accepted_stake=None,
    )

    evidence = build_empirical_execution_evidence(
        ledger,
        attempt_id="attempt-1",
    )

    assert evidence.attempt_state == "REJECTED"
    assert evidence.slippage_status == SLIPPAGE_STATUS_NOT_APPLICABLE
    assert evidence.accepted_odds is None
    assert evidence.adverse_odds_delta is None
    assert evidence.unaccepted_stake is None


def test_lay_higher_accepted_odds_are_adverse(tmp_path):
    ledger = _ledger(
        tmp_path,
        action=_action(side="LAY", requested_odds=Decimal("3.00")),
    )
    _bind_provider(ledger)
    _ack(ledger, accepted_odds=Decimal("3.05"))

    evidence = build_empirical_execution_evidence(
        ledger,
        attempt_id="attempt-1",
    )

    assert evidence.accepted_minus_requested_odds == Decimal("0.05")
    assert evidence.adverse_odds_delta == Decimal("0.05")


def test_unsupported_side_cannot_mint_known_slippage_semantics(tmp_path):
    ledger = _ledger(tmp_path, action=_action(side="CUSTOM"))
    _bind_provider(ledger)
    _ack(ledger)

    with pytest.raises(
        EmpiricalExecutionEvidenceUnavailable,
        match="BACK/LAY",
    ):
        build_empirical_execution_evidence(
            ledger,
            attempt_id="attempt-1",
        )


def test_wall_clock_cross_stage_inversion_is_not_reported_as_latency(tmp_path):
    ledger = _ledger(tmp_path)
    _bind_provider(
        ledger,
        observed_at="2026-09-21T10:00:01.900000+00:00",
    )
    _ack(
        ledger,
        acknowledged_at="2026-09-21T10:00:01.800000+00:00",
    )

    evidence = build_empirical_execution_evidence(
        ledger,
        attempt_id="attempt-1",
    )

    assert evidence.provider_evidence_observed_at > evidence.acknowledged_at
    assert evidence.causal_timing_status == TIMING_STATUS_UNKNOWN
    assert evidence.submit_to_provider_evidence_us is None
    assert evidence.submit_to_acknowledgement_us is None


def test_direct_construction_cannot_turn_wall_timestamps_into_latency(tmp_path):
    evidence = _accepted_evidence(tmp_path)

    with pytest.raises(
        EmpiricalExecutionEvidenceError,
        match="wall-clock timestamps cannot mint causal latency",
    ):
        replace(evidence, decision_to_submit_us=250_000)


def test_direct_construction_cannot_drop_nonterminal_censoring(tmp_path):
    ledger = _ledger(tmp_path)
    evidence = build_empirical_execution_evidence(
        ledger,
        attempt_id="attempt-1",
    )

    with pytest.raises(
        EmpiricalExecutionEvidenceError,
        match="nonterminal attempt must be explicitly right-censored",
    ):
        replace(evidence, right_censored=False)


def test_direct_construction_rejects_slippage_forgery(tmp_path):
    evidence = _accepted_evidence(tmp_path)

    with pytest.raises(
        EmpiricalExecutionEvidenceError,
        match="adverse odds delta is inconsistent",
    ):
        replace(evidence, adverse_odds_delta=Decimal("0.01"))


def test_restart_rebuild_is_byte_identical_for_terminal_record(tmp_path):
    ledger = _ledger(tmp_path)
    _bind_provider(ledger)
    _ack(ledger)
    first = build_empirical_execution_evidence(
        ledger,
        attempt_id="attempt-1",
    )

    restarted = RealExecutionLedger(ledger.path)
    second = build_empirical_execution_evidence(
        restarted,
        attempt_id="attempt-1",
    )

    assert second.to_dict() == first.to_dict()
    assert second.evidence_sha256 == first.evidence_sha256


def test_restart_rebuild_is_byte_identical_for_censored_record(tmp_path):
    ledger = _ledger(tmp_path)
    first = build_empirical_execution_evidence(
        ledger,
        attempt_id="attempt-1",
    )

    restarted = RealExecutionLedger(ledger.path)
    second = build_empirical_execution_evidence(
        restarted,
        attempt_id="attempt-1",
    )

    assert second.to_dict() == first.to_dict()
    assert second.evidence_sha256 == first.evidence_sha256


def test_missing_attempt_is_unavailable_not_synthetic_censor_record(tmp_path):
    ledger = RealExecutionLedger(tmp_path / "execution.jsonl")

    with pytest.raises(
        EmpiricalExecutionEvidenceUnavailable,
        match="attempt is not present",
    ):
        build_empirical_execution_evidence(
            ledger,
            attempt_id="missing",
        )


def test_tampered_ledger_fails_before_empirical_projection(tmp_path):
    ledger = _ledger(tmp_path)
    _bind_provider(ledger)
    _ack(ledger)
    raw = ledger.path.read_text(encoding="utf-8")
    assert "receipt-1" in raw
    ledger.path.write_text(
        raw.replace("receipt-1", "receipt-x", 1),
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(ExecutionLedgerIntegrityError):
        build_empirical_execution_evidence(
            ledger,
            attempt_id="attempt-1",
        )
