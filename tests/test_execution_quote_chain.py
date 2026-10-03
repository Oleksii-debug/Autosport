from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from autosport.execution_quote_chain import (
    ACCEPTED_PRICE_ACKNOWLEDGED_UNVERIFIED,
    ACCEPTED_PRICE_NOT_APPLICABLE,
    ACCEPTED_PRICE_UNKNOWN,
    CHAIN_NOT_SUBMITTED,
    CHAIN_SUBMISSION_UNKNOWN,
    CHAIN_SUBMIT_INSTRUCTION_UNBOUND,
    CHAIN_SUBMIT_INSTRUCTION_BOUND,
    ExecutionQuoteChainError,
    ExecutionQuoteChainUnavailable,
    build_execution_quote_chain_evidence,
)
from autosport.real_execution_ledger import (
    AcknowledgementStatus,
    ExecutionAction,
    ExecutionPlan,
    ExternalAcknowledgement,
    RealExecutionLedger,
    ReconciliationSnapshot,
)


QUOTE = "2026-10-03T12:00:00+00:00"
DECISION = "2026-10-03T12:00:01+00:00"
RESERVED = "2026-10-03T12:00:01.100000+00:00"
SUBMITTED = "2026-10-03T12:00:01.200000+00:00"
PROVIDER = "2026-10-03T12:00:01.500000+00:00"
ACKED = "2026-10-03T12:00:01.600000+00:00"
EVIDENCE_ID = "e" * 64


def _action(
    *,
    action_id: str = "action-1",
    quote_id: str = "decision-quote-1",
    requested_odds: Decimal = Decimal("2.10"),
    requested_stake: Decimal = Decimal("10.00"),
) -> ExecutionAction:
    return ExecutionAction(
        action_id=action_id,
        bookmaker_id="provider-1",
        account_id="account-1",
        event_id="event-1",
        market_id="market-1",
        selection_id="selection-1",
        side="BACK",
        requested_odds=requested_odds,
        requested_stake=requested_stake,
        quote_id=quote_id,
        quote_observed_at=QUOTE,
        expires_at="2026-10-03T12:01:00+00:00",
    )


def _reserved(tmp_path, *, action: ExecutionAction | None = None) -> RealExecutionLedger:
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


def _submitted(tmp_path, *, action: ExecutionAction | None = None) -> RealExecutionLedger:
    ledger = _reserved(tmp_path, action=action)
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
    odds: Decimal | None = Decimal("2.08"),
    stake: Decimal | None = Decimal("10.00"),
) -> None:
    ledger.acknowledge(
        ExternalAcknowledgement(
            attempt_id="attempt-1",
            external_receipt_id="receipt-1",
            status=status,
            acknowledged_at=ACKED,
            accepted_odds=odds,
            accepted_stake=stake,
        )
    )


def _project(ledger: RealExecutionLedger):
    return build_execution_quote_chain_evidence(
        ledger,
        plan_id="plan-1",
        attempt_id="attempt-1",
    )


def test_reserved_attempt_is_explicitly_not_submitted(tmp_path) -> None:
    evidence = _project(_reserved(tmp_path))

    assert evidence.attempt_state == "RESERVED"
    assert evidence.chain_status == CHAIN_NOT_SUBMITTED
    assert evidence.submitted_at is None
    assert evidence.submission_instruction_sha256 is None
    assert evidence.actual_submitted_instruction_bound is False
    assert evidence.accepted_price_status == ACCEPTED_PRICE_UNKNOWN
    assert evidence.chain_complete is False


def test_submitted_attempt_does_not_launder_requested_action_into_submit_truth(
    tmp_path,
) -> None:
    evidence = _project(_submitted(tmp_path))

    assert evidence.attempt_state == "SUBMITTED"
    assert evidence.requested_odds == Decimal("2.10")
    assert evidence.decision_quote_id == "decision-quote-1"
    assert evidence.submitted_at == SUBMITTED
    assert evidence.submission_instruction_sha256 is None
    assert evidence.chain_status == CHAIN_SUBMIT_INSTRUCTION_UNBOUND
    assert evidence.submit_instruction_identity_bound is False
    assert evidence.provider_request_correlation_bound is False
    assert evidence.actual_submitted_instruction_bound is False
    assert evidence.chain_complete is False


def test_numeric_equal_acknowledgement_still_cannot_complete_chain(tmp_path) -> None:
    ledger = _submitted(tmp_path)
    _bind_provider(ledger)
    _ack(ledger, odds=Decimal("2.10"), stake=Decimal("10.00"))

    evidence = _project(ledger)

    assert evidence.acknowledged_odds == evidence.requested_odds
    assert evidence.acknowledged_stake == evidence.requested_stake
    assert evidence.accepted_price_status == ACCEPTED_PRICE_ACKNOWLEDGED_UNVERIFIED
    assert evidence.accepted_price_verified is False
    assert evidence.chain_status == CHAIN_SUBMIT_INSTRUCTION_UNBOUND
    assert evidence.chain_complete is False


def test_different_acknowledged_odds_remain_distinct_from_decision_request(
    tmp_path,
) -> None:
    ledger = _submitted(tmp_path)
    _bind_provider(ledger)
    _ack(ledger, odds=Decimal("2.06"), stake=Decimal("7.50"))

    evidence = _project(ledger)

    assert evidence.requested_odds == Decimal("2.10")
    assert evidence.requested_stake == Decimal("10.00")
    assert evidence.acknowledged_odds == Decimal("2.06")
    assert evidence.acknowledged_stake == Decimal("7.50")
    assert evidence.accepted_price_status == ACCEPTED_PRICE_ACKNOWLEDGED_UNVERIFIED
    assert evidence.accepted_price_verified is False


def test_partial_acknowledgement_preserves_quantity_without_promoting_price(
    tmp_path,
) -> None:
    ledger = _submitted(tmp_path)
    _bind_provider(ledger)
    _ack(
        ledger,
        status=AcknowledgementStatus.PARTIAL,
        odds=Decimal("2.08"),
        stake=Decimal("4.00"),
    )

    evidence = _project(ledger)

    assert evidence.attempt_state == "PARTIAL"
    assert evidence.acknowledgement_status == "PARTIAL"
    assert evidence.acknowledged_odds == Decimal("2.08")
    assert evidence.acknowledged_stake == Decimal("4.00")
    assert evidence.requested_stake == Decimal("10.00")
    assert evidence.accepted_price_status == ACCEPTED_PRICE_ACKNOWLEDGED_UNVERIFIED
    assert evidence.chain_complete is False


def test_rejected_acknowledgement_has_no_accepted_price_claim(tmp_path) -> None:
    ledger = _submitted(tmp_path)
    _bind_provider(ledger)
    _ack(
        ledger,
        status=AcknowledgementStatus.REJECTED,
        odds=None,
        stake=None,
    )

    evidence = _project(ledger)

    assert evidence.attempt_state == "REJECTED"
    assert evidence.acknowledgement_status == "REJECTED"
    assert evidence.acknowledged_odds is None
    assert evidence.acknowledged_stake is None
    assert evidence.accepted_price_status == ACCEPTED_PRICE_NOT_APPLICABLE
    assert evidence.accepted_price_verified is False
    assert evidence.chain_complete is False


def test_unknown_before_durable_submit_is_explicit_submission_uncertainty(
    tmp_path,
) -> None:
    ledger = _reserved(tmp_path)
    ledger.mark_unknown(
        "attempt-1",
        reason="process_restart_before_submit_boundary_was_proven",
        observed_at="2026-10-03T12:00:02+00:00",
    )

    evidence = _project(ledger)

    assert evidence.attempt_state == "UNKNOWN"
    assert evidence.submitted_at is None
    assert evidence.chain_status == CHAIN_SUBMISSION_UNKNOWN
    assert evidence.submission_instruction_sha256 is None
    assert evidence.actual_submitted_instruction_bound is False
    assert evidence.accepted_price_status == ACCEPTED_PRICE_UNKNOWN
    assert evidence.accepted_price_verified is False
    assert evidence.chain_complete is False


def test_not_found_after_unknown_before_submit_does_not_become_not_submitted(
    tmp_path,
) -> None:
    ledger = _reserved(tmp_path)
    ledger.mark_unknown(
        "attempt-1",
        reason="process_restart_before_submit_boundary_was_proven",
        observed_at="2026-10-03T12:00:02+00:00",
    )
    ledger.reconcile_not_found(
        ReconciliationSnapshot(
            attempt_id="attempt-1",
            evidence_id="n" * 64,
            observed_at="2026-10-03T12:00:03+00:00",
            external_effect_found=False,
            source="provider-order-history",
        )
    )

    evidence = _project(ledger)

    assert evidence.attempt_state == "RECONCILED_NOT_FOUND"
    assert evidence.submitted_at is None
    assert evidence.chain_status == CHAIN_SUBMISSION_UNKNOWN
    assert evidence.actual_submitted_instruction_bound is False
    assert evidence.accepted_price_verified is False
    assert evidence.chain_complete is False


def test_unknown_attempt_cannot_invent_submit_instruction_identity(tmp_path) -> None:
    ledger = _submitted(tmp_path)
    ledger.mark_unknown(
        "attempt-1",
        reason="provider timeout after possible external effect",
        observed_at="2026-10-03T12:00:02+00:00",
    )

    evidence = _project(ledger)

    assert evidence.attempt_state == "UNKNOWN"
    assert evidence.submitted_at == SUBMITTED
    assert evidence.chain_status == CHAIN_SUBMIT_INSTRUCTION_UNBOUND
    assert evidence.submission_instruction_sha256 is None
    assert evidence.accepted_price_status == ACCEPTED_PRICE_UNKNOWN


def test_projection_uses_frozen_decision_quote_from_durable_plan(tmp_path) -> None:
    action = _action(quote_id="quote-at-decision", requested_odds=Decimal("1.91"))
    ledger = _submitted(tmp_path, action=action)

    evidence = _project(ledger)

    assert evidence.decision_quote_id == "quote-at-decision"
    assert evidence.decision_quote_observed_at == QUOTE
    assert evidence.requested_odds == Decimal("1.91")
    assert "later-quote" not in str(evidence.to_dict())


def test_provider_correlation_is_visible_but_not_submit_or_price_authority(
    tmp_path,
) -> None:
    ledger = _submitted(tmp_path)
    _bind_provider(ledger)

    evidence = _project(ledger)

    assert evidence.provider_evidence_id == EVIDENCE_ID
    assert evidence.provider_evidence_source == "provider-response"
    assert evidence.provider_evidence_observed_at == PROVIDER
    assert evidence.provider_acknowledgement_sha256 is None
    assert evidence.acknowledgement_binding_matches is False
    assert evidence.actual_submitted_instruction_bound is False
    assert evidence.accepted_price_verified is False


def test_effect_fingerprint_is_retained_but_not_mislabeled_submit_payload_hash(
    tmp_path,
) -> None:
    evidence = _project(_submitted(tmp_path))

    assert len(evidence.effect_fingerprint) == 64
    assert evidence.submission_instruction_sha256 is None
    assert evidence.to_dict()["effect_fingerprint"] == evidence.effect_fingerprint
    assert evidence.to_dict()["submission_instruction_sha256"] is None


def test_cross_plan_attempt_lookup_fails_closed(tmp_path) -> None:
    ledger = _submitted(tmp_path)

    with pytest.raises(ExecutionQuoteChainUnavailable, match="attempt"):
        build_execution_quote_chain_evidence(
            ledger,
            plan_id="plan-1",
            attempt_id="other-attempt",
        )

    with pytest.raises(ExecutionQuoteChainUnavailable, match="plan"):
        build_execution_quote_chain_evidence(
            ledger,
            plan_id="other-plan",
            attempt_id="attempt-1",
        )


def test_noncanonical_ledger_subclass_is_rejected(tmp_path) -> None:
    class DerivedLedger(RealExecutionLedger):
        pass

    ledger = DerivedLedger(tmp_path / "derived.jsonl")

    with pytest.raises(TypeError, match="canonical RealExecutionLedger"):
        build_execution_quote_chain_evidence(
            ledger,
            plan_id="plan-1",
            attempt_id="attempt-1",
        )


def test_projection_digest_is_restart_stable_for_identical_ledger_bytes(
    tmp_path,
) -> None:
    ledger = _submitted(tmp_path)
    _bind_provider(ledger)
    first = _project(ledger)

    reopened = RealExecutionLedger(ledger.path)
    second = _project(reopened)

    assert second.source_ledger_sha256 == first.source_ledger_sha256
    assert second.source_event_count == first.source_event_count
    assert second.to_dict() == first.to_dict()
    assert second.evidence_sha256 == first.evidence_sha256


def test_later_durable_fact_changes_snapshot_not_historical_request(tmp_path) -> None:
    ledger = _submitted(tmp_path)
    before = _project(ledger)

    _bind_provider(ledger)
    after = _project(ledger)

    assert after.source_ledger_sha256 != before.source_ledger_sha256
    assert after.source_event_count == before.source_event_count + 1
    assert after.evidence_sha256 != before.evidence_sha256
    assert after.decision_quote_id == before.decision_quote_id
    assert after.requested_odds == before.requested_odds
    assert after.effect_fingerprint == before.effect_fingerprint
    assert after.chain_complete is False


def test_evidence_invariant_rejects_not_submitted_label_for_unknown_state(
    tmp_path,
) -> None:
    ledger = _reserved(tmp_path)
    ledger.mark_unknown(
        "attempt-1",
        reason="ambiguous submit boundary",
        observed_at="2026-10-03T12:00:02+00:00",
    )
    evidence = _project(ledger)

    with pytest.raises(ExecutionQuoteChainError, match="chain_status"):
        replace(evidence, chain_status=CHAIN_NOT_SUBMITTED)


def test_evidence_shape_has_no_caller_settable_positive_chain_flags(tmp_path) -> None:
    evidence = _project(_submitted(tmp_path))
    payload = evidence.to_dict()

    assert payload["actual_submitted_instruction_bound"] is False
    assert payload["accepted_price_verified"] is False
    assert payload["chain_complete"] is False
    assert payload["chain_status"] == CHAIN_SUBMIT_INSTRUCTION_UNBOUND

    with pytest.raises(ExecutionQuoteChainError, match="chain_status"):
        replace(evidence, chain_status=CHAIN_NOT_SUBMITTED)


def test_acknowledgement_observation_cannot_be_reinterpreted_as_verified_price(
    tmp_path,
) -> None:
    ledger = _submitted(tmp_path)
    _bind_provider(ledger)
    _ack(ledger)

    evidence = _project(ledger)
    payload = evidence.to_dict()

    assert payload["acknowledged_odds"] == "2.08"
    assert payload["acknowledged_stake"] == "10"
    assert payload["accepted_price_status"] == ACCEPTED_PRICE_ACKNOWLEDGED_UNVERIFIED
    assert payload["accepted_price_verified"] is False
    assert payload["chain_complete"] is False


def test_evidence_digest_covers_negative_authority_state(tmp_path) -> None:
    evidence = _project(_submitted(tmp_path))
    payload = evidence.to_dict()

    assert payload["evidence_sha256"] == evidence.evidence_sha256
    assert len(evidence.evidence_sha256) == 64
    assert payload["submission_instruction_sha256"] is None
    assert payload["actual_submitted_instruction_bound"] is False
    assert payload["accepted_price_verified"] is False
    assert payload["chain_complete"] is False

def test_successor_request_binding_advances_request_truth_without_promoting_price(
    tmp_path,
) -> None:
    ledger = _submitted(tmp_path)
    _bind_provider(ledger)
    evidence = _project(ledger)
    request_sha256 = "a" * 64

    successor_shape = replace(
        evidence,
        submission_instruction_sha256=request_sha256,
        provider_request_sha256=request_sha256,
        chain_status=CHAIN_SUBMIT_INSTRUCTION_BOUND,
    )

    assert successor_shape.submit_instruction_identity_bound is True
    assert successor_shape.provider_request_correlation_bound is True
    assert successor_shape.actual_submitted_instruction_bound is True
    assert successor_shape.accepted_price_status == ACCEPTED_PRICE_UNKNOWN
    assert successor_shape.accepted_price_verified is False
    assert successor_shape.chain_complete is False
    payload = successor_shape.to_dict()
    assert payload["submission_instruction_sha256"] == request_sha256
    assert payload["provider_request_sha256"] == request_sha256


def test_provider_request_digest_cannot_conflict_with_durable_submission(
    tmp_path,
) -> None:
    ledger = _submitted(tmp_path)
    _bind_provider(ledger)
    evidence = _project(ledger)

    with pytest.raises(
        ExecutionQuoteChainError,
        match="provider request digest conflicts with durable submission",
    ):
        replace(
            evidence,
            submission_instruction_sha256="a" * 64,
            provider_request_sha256="b" * 64,
            chain_status=CHAIN_SUBMIT_INSTRUCTION_BOUND,
        )


def test_future_canonical_rejected_ack_may_omit_external_receipt(
    tmp_path,
) -> None:
    ledger = _submitted(tmp_path)
    _bind_provider(ledger)
    _ack(
        ledger,
        status=AcknowledgementStatus.REJECTED,
        odds=None,
        stake=None,
    )
    evidence = _project(ledger)

    successor_shape = replace(evidence, external_receipt_id=None)

    assert successor_shape.acknowledgement_status == "REJECTED"
    assert successor_shape.external_receipt_id is None
    assert successor_shape.accepted_price_status == ACCEPTED_PRICE_NOT_APPLICABLE
    assert successor_shape.accepted_price_verified is False
    assert successor_shape.chain_complete is False

