from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from autosport.execution_empirical_evidence import (
    EmpiricalExecutionEvidenceError,
    EmpiricalExecutionEvidenceUnavailable,
    build_empirical_execution_evidence,
)
from autosport.real_execution_ledger import (
    AcknowledgementStatus,
    ExecutionAction,
    ExecutionPlan,
    ExternalAcknowledgement,
    RealExecutionLedger,
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


def _ledger(tmp_path, *, action: ExecutionAction | None = None) -> RealExecutionLedger:
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
    ledger.mark_submitted("attempt-1", submitted_at=SUBMITTED)
    return ledger


def _bind_provider(ledger: RealExecutionLedger) -> None:
    ledger.bind_provider_evidence(
        attempt_id="attempt-1",
        evidence_id=EVIDENCE_ID,
        observed_at=PROVIDER,
        source="provider-response",
    )


def _ack(
    ledger: RealExecutionLedger,
    *,
    status: AcknowledgementStatus = AcknowledgementStatus.ACCEPTED,
    accepted_odds: Decimal | None = Decimal("2.08"),
    accepted_stake: Decimal | None = Decimal("5.00"),
) -> None:
    ledger.acknowledge(
        ExternalAcknowledgement(
            attempt_id="attempt-1",
            external_receipt_id="receipt-1",
            status=status,
            acknowledged_at=ACKED,
            accepted_odds=accepted_odds,
            accepted_stake=accepted_stake,
        )
    )


def _complete_evidence(tmp_path):
    ledger = _ledger(tmp_path)
    _bind_provider(ledger)
    _ack(ledger)
    return build_empirical_execution_evidence(ledger, attempt_id="attempt-1")


def test_builds_exact_latency_and_adverse_back_slippage_from_durable_facts(
    tmp_path,
):
    ledger = _ledger(tmp_path)
    _bind_provider(ledger)
    _ack(ledger, accepted_odds=Decimal("2.08"))

    evidence = build_empirical_execution_evidence(
        ledger,
        attempt_id="attempt-1",
    )

    assert evidence.quote_age_at_decision_us == 1_000_000
    assert evidence.decision_to_reserve_us == 100_000
    assert evidence.decision_to_submit_us == 250_000
    assert evidence.submit_to_provider_evidence_us == 450_000
    assert evidence.submit_to_acknowledgement_us == 550_000
    assert evidence.decision_to_acknowledgement_us == 800_000
    assert evidence.accepted_minus_requested_odds == Decimal("-0.02")
    assert evidence.adverse_odds_delta == Decimal("0.02")
    assert evidence.unaccepted_stake == Decimal("0.00")
    assert evidence.provider_evidence_id == EVIDENCE_ID
    assert evidence.to_dict()["evidence_sha256"] == evidence.evidence_sha256


def test_favorable_back_price_has_zero_adverse_delta(tmp_path):
    ledger = _ledger(tmp_path)
    _bind_provider(ledger)
    _ack(ledger, accepted_odds=Decimal("2.12"))

    evidence = build_empirical_execution_evidence(
        ledger,
        attempt_id="attempt-1",
    )

    assert evidence.accepted_minus_requested_odds == Decimal("0.02")
    assert evidence.adverse_odds_delta == Decimal("0")


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

    assert evidence.acknowledgement_status == "PARTIAL"
    assert evidence.unaccepted_stake == Decimal("1.75")
    assert evidence.adverse_odds_delta == Decimal("0")


def test_rejected_attempt_has_latency_evidence_without_price_claim(tmp_path):
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

    assert evidence.acknowledgement_status == "REJECTED"
    assert evidence.accepted_odds is None
    assert evidence.adverse_odds_delta is None
    assert evidence.unaccepted_stake is None


def test_positive_measurement_requires_durable_provider_evidence(tmp_path):
    ledger = _ledger(tmp_path)
    _ack(ledger)

    with pytest.raises(
        EmpiricalExecutionEvidenceUnavailable,
        match="PROVIDER_EVIDENCE_BOUND",
    ):
        build_empirical_execution_evidence(
            ledger,
            attempt_id="attempt-1",
        )


def test_nonterminal_attempt_is_not_empirical_execution_evidence(tmp_path):
    ledger = _ledger(tmp_path)
    _bind_provider(ledger)

    with pytest.raises(
        EmpiricalExecutionEvidenceUnavailable,
        match="terminal durable acknowledgement",
    ):
        build_empirical_execution_evidence(
            ledger,
            attempt_id="attempt-1",
        )


def test_negative_decision_to_reserve_timing_fails_closed(tmp_path):
    ledger = RealExecutionLedger(tmp_path / "execution.jsonl")
    action = _action()
    plan = ExecutionPlan(
        plan_id="plan-1",
        bookmaker_profile_version="profile-1",
        decision_id="decision-1",
        approval_id="approval-1",
        created_at=DECISION,
        actions=(action,),
    )
    ledger.reserve_plan(plan)
    ledger.begin_attempt(
        plan_id="plan-1",
        action_id="leg-1",
        attempt_id="attempt-1",
        reserved_at="2026-09-21T10:00:00.500000+00:00",
    )
    ledger.mark_submitted("attempt-1", submitted_at=SUBMITTED)
    _bind_provider(ledger)
    _ack(ledger)

    with pytest.raises(
        EmpiricalExecutionEvidenceUnavailable,
        match="decision_to_reserve is negative",
    ):
        build_empirical_execution_evidence(
            ledger,
            attempt_id="attempt-1",
        )


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


def test_unsupported_side_cannot_mint_slippage_semantics(tmp_path):
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


def test_direct_construction_rejects_negative_adverse_delta(tmp_path):
    evidence = _complete_evidence(tmp_path)

    with pytest.raises(
        EmpiricalExecutionEvidenceError,
        match="adverse odds delta is inconsistent",
    ):
        replace(evidence, adverse_odds_delta=Decimal("-0.01"))


def test_direct_construction_rejects_timestamp_metric_forgery(tmp_path):
    evidence = _complete_evidence(tmp_path)

    with pytest.raises(
        EmpiricalExecutionEvidenceError,
        match="decision_to_submit_us does not match bound timestamps",
    ):
        replace(evidence, decision_to_submit_us=evidence.decision_to_submit_us + 1)


def test_direct_construction_rejects_rejected_metric_claim(tmp_path):
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

    with pytest.raises(
        EmpiricalExecutionEvidenceError,
        match="rejected evidence cannot claim accepted/slippage metrics",
    ):
        replace(evidence, accepted_odds=Decimal("2.10"))
