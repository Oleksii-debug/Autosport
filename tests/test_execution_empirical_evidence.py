from __future__ import annotations

from copy import copy
from dataclasses import replace
from decimal import Decimal

import pytest

from autosport.execution_empirical_evidence import (
    EmpiricalExecutionEvidenceError,
    EmpiricalExecutionEvidenceUnavailable,
    EmpiricalExecutionPopulationEvidence,
    EVALUATION_PROTOCOL_AUTHORITY_UNQUALIFIED,
    PROVIDER_OUTCOME_NOT_APPLICABLE,
    PROVIDER_OUTCOME_UNVERIFIED_ABSENCE,
    PROVIDER_OUTCOME_UNVERIFIED_ACK,
    SOURCE_ROOT_AUTHORITY_UNQUALIFIED,
    SLIPPAGE_STATUS_KNOWN,
    SLIPPAGE_STATUS_NOT_APPLICABLE,
    SLIPPAGE_STATUS_UNKNOWN,
    TIMING_REASON_NO_MONOTONIC_WITNESS,
    TIMING_STATUS_UNKNOWN,
    build_empirical_execution_evidence,
    build_empirical_execution_population_evidence,
)
from autosport.real_execution_ledger import (
    AcknowledgementStatus,
    ExecutionAction,
    ExecutionLedgerIntegrityError,
    ExecutionPlan,
    ExecutionStateError,
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
RECONCILIATION_ID = "r" * 64
RECONCILIATION_AT = "2026-09-21T10:00:03+00:00"
RECONCILIATION_SOURCE = "provider-order-readback"


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


def _reconciled_not_found_ledger(tmp_path) -> RealExecutionLedger:
    ledger = _ledger(tmp_path)
    ledger.mark_unknown(
        "attempt-1",
        reason="provider timeout after submission",
        observed_at="2026-09-21T10:00:02+00:00",
    )
    ledger.reconcile_not_found(
        ReconciliationSnapshot(
            attempt_id="attempt-1",
            evidence_id=RECONCILIATION_ID,
            observed_at=RECONCILIATION_AT,
            external_effect_found=False,
            source=RECONCILIATION_SOURCE,
        )
    )
    return ledger


def _accepted_evidence(tmp_path):
    ledger = _ledger(tmp_path)
    _bind_provider(ledger)
    _ack(ledger)
    return build_empirical_execution_evidence(
        ledger,
        attempt_id="attempt-1",
    )


def test_terminal_accepted_record_keeps_provider_correlation_but_slippage_unknown(tmp_path):
    evidence = _accepted_evidence(tmp_path)

    assert evidence.attempt_state == "ACCEPTED"
    assert evidence.ledger_terminal is True
    assert evidence.provider_outcome_verified is False
    assert (
        evidence.provider_outcome_verification_reason
        == PROVIDER_OUTCOME_UNVERIFIED_ACK
    )
    assert evidence.source_product_authority_verified is False
    assert evidence.source_root_authority_status == SOURCE_ROOT_AUTHORITY_UNQUALIFIED
    assert evidence.right_censored is False
    assert evidence.censor_reason is None
    assert evidence.censor_cutoff_recorded_at is None
    assert evidence.provider_evidence_id == EVIDENCE_ID
    assert evidence.provider_evidence_observed_at == PROVIDER

    assert evidence.slippage_status == SLIPPAGE_STATUS_UNKNOWN
    assert evidence.accepted_odds is None
    assert evidence.accepted_stake is None
    assert evidence.accepted_minus_requested_odds is None
    assert evidence.adverse_odds_delta is None
    assert evidence.unaccepted_stake is None

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
    assert evidence.ledger_terminal is False
    assert evidence.provider_outcome_verified is False
    assert (
        evidence.provider_outcome_verification_reason
        == PROVIDER_OUTCOME_NOT_APPLICABLE
    )
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
    assert evidence.provider_outcome_verified is False
    assert (
        evidence.provider_outcome_verification_reason
        == PROVIDER_OUTCOME_NOT_APPLICABLE
    )
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
    assert evidence.provider_outcome_verified is False
    assert (
        evidence.provider_outcome_verification_reason
        == PROVIDER_OUTCOME_NOT_APPLICABLE
    )
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
    assert evidence.ledger_terminal is True
    assert evidence.provider_evidence_id is None
    assert evidence.provider_outcome_verified is False
    assert (
        evidence.provider_outcome_verification_reason
        == PROVIDER_OUTCOME_UNVERIFIED_ACK
    )
    assert evidence.slippage_status == SLIPPAGE_STATUS_UNKNOWN
    assert evidence.accepted_odds is None
    assert evidence.adverse_odds_delta is None


def test_reconciled_not_found_is_unverified_right_censored_evidence(tmp_path):
    ledger = _reconciled_not_found_ledger(tmp_path)

    evidence = build_empirical_execution_evidence(
        ledger,
        attempt_id="attempt-1",
    )

    assert evidence.attempt_state == "RECONCILED_NOT_FOUND"
    assert evidence.ledger_terminal is False
    assert evidence.provider_outcome_verified is False
    assert (
        evidence.provider_outcome_verification_reason
        == PROVIDER_OUTCOME_UNVERIFIED_ABSENCE
    )
    assert evidence.right_censored is True
    assert (
        evidence.censor_reason
        == "RECONCILED_NOT_FOUND_UNVERIFIED_ABSENCE_AUTHORITY"
    )
    assert evidence.censor_cutoff_recorded_at is not None
    assert evidence.censor_cutoff_event_count == evidence.source_event_count
    assert evidence.acknowledgement_status is None
    assert evidence.acknowledged_at is None
    assert evidence.external_receipt_id is None
    assert evidence.reconciliation_evidence_id == RECONCILIATION_ID
    assert evidence.reconciliation_evidence_source == RECONCILIATION_SOURCE
    assert evidence.reconciliation_evidence_observed_at == RECONCILIATION_AT
    assert evidence.reconciliation_external_effect_found is False
    assert evidence.slippage_status == SLIPPAGE_STATUS_UNKNOWN
    assert evidence.accepted_odds is None


def test_reconciled_not_found_direct_construction_cannot_forge_positive_effect(tmp_path):
    evidence = build_empirical_execution_evidence(
        _reconciled_not_found_ledger(tmp_path),
        attempt_id="attempt-1",
    )

    with pytest.raises(
        EmpiricalExecutionEvidenceError,
        match="external_effect_found=false",
    ):
        replace(evidence, reconciliation_external_effect_found=True)


def test_reconciled_not_found_direct_construction_requires_reconciliation_identity(
    tmp_path,
):
    evidence = build_empirical_execution_evidence(
        _reconciled_not_found_ledger(tmp_path),
        attempt_id="attempt-1",
    )

    with pytest.raises(
        EmpiricalExecutionEvidenceError,
        match="all present or all absent",
    ):
        replace(evidence, reconciliation_evidence_id=None)


def test_generic_provider_evidence_cannot_mint_known_slippage_by_direct_construction(
    tmp_path,
):
    evidence = _accepted_evidence(tmp_path)
    assert evidence.provider_evidence_id == EVIDENCE_ID

    with pytest.raises(
        EmpiricalExecutionEvidenceError,
        match="typed accepted-price provider evidence",
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


def test_partial_acceptance_keeps_unproven_slippage_unknown(tmp_path):
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
    assert evidence.ledger_terminal is True
    assert evidence.provider_outcome_verified is False
    assert (
        evidence.provider_outcome_verification_reason
        == PROVIDER_OUTCOME_UNVERIFIED_ACK
    )
    assert evidence.acknowledgement_status == "PARTIAL"
    assert evidence.provider_evidence_id == EVIDENCE_ID
    assert evidence.slippage_status == SLIPPAGE_STATUS_UNKNOWN
    assert evidence.accepted_odds is None
    assert evidence.accepted_stake is None
    assert evidence.unaccepted_stake is None
    assert evidence.adverse_odds_delta is None


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
    assert evidence.ledger_terminal is True
    assert evidence.provider_outcome_verified is False
    assert (
        evidence.provider_outcome_verification_reason
        == PROVIDER_OUTCOME_UNVERIFIED_ACK
    )
    assert evidence.slippage_status == SLIPPAGE_STATUS_NOT_APPLICABLE
    assert evidence.accepted_odds is None
    assert evidence.adverse_odds_delta is None
    assert evidence.unaccepted_stake is None


def test_generic_provider_evidence_cannot_mint_lay_slippage(tmp_path):
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

    assert evidence.slippage_status == SLIPPAGE_STATUS_UNKNOWN
    assert evidence.accepted_minus_requested_odds is None
    assert evidence.adverse_odds_delta is None


def test_generic_provider_evidence_does_not_require_price_side_semantics(tmp_path):
    ledger = _ledger(tmp_path, action=_action(side="CUSTOM"))
    _bind_provider(ledger)
    _ack(ledger)

    evidence = build_empirical_execution_evidence(
        ledger,
        attempt_id="attempt-1",
    )

    assert evidence.provider_evidence_id == EVIDENCE_ID
    assert evidence.slippage_status == SLIPPAGE_STATUS_UNKNOWN
    assert evidence.accepted_odds is None
    assert evidence.adverse_odds_delta is None


def test_caller_ack_cannot_upgrade_provider_outcome_authority(tmp_path):
    evidence = _accepted_evidence(tmp_path)

    with pytest.raises(
        EmpiricalExecutionEvidenceError,
        match="cannot verify provider outcome provenance",
    ):
        replace(evidence, provider_outcome_verified=True)

    with pytest.raises(
        EmpiricalExecutionEvidenceError,
        match="verification reason mismatches durable attempt state",
    ):
        replace(
            evidence,
            provider_outcome_verification_reason=PROVIDER_OUTCOME_NOT_APPLICABLE,
        )


def test_wall_clock_cross_stage_inversion_is_rejected_before_projection(tmp_path):
    ledger = _ledger(tmp_path)
    _bind_provider(
        ledger,
        observed_at="2026-09-21T10:00:01.900000+00:00",
    )

    with pytest.raises(
        ExecutionStateError,
        match="acknowledgement precedes attempt causal boundary",
    ):
        _ack(
            ledger,
            acknowledged_at="2026-09-21T10:00:01.800000+00:00",
        )


def test_builder_issuance_does_not_mint_product_root_provenance(tmp_path):
    evidence = _accepted_evidence(tmp_path)

    evidence.assert_projection_issued()
    payload = evidence.to_dict()
    assert payload["source_product_authority_verified"] is False
    assert (
        payload["source_root_authority_status"]
        == SOURCE_ROOT_AUTHORITY_UNQUALIFIED
    )

    with pytest.raises(
        EmpiricalExecutionEvidenceError,
        match="does not verify product-owned ledger/workspace authority",
    ):
        replace(evidence, source_product_authority_verified=True)

    with pytest.raises(
        EmpiricalExecutionEvidenceError,
        match="does not verify product-owned ledger/workspace authority",
    ):
        replace(evidence, source_root_authority_status="PRODUCT_ROOT_VERIFIED")


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
        match="UNKNOWN slippage cannot claim accepted-price metrics",
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


def test_restart_rebuild_is_byte_identical_for_reconciled_not_found(tmp_path):
    ledger = _reconciled_not_found_ledger(tmp_path)
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
    assert second.ledger_terminal is False
    assert second.right_censored is True
    assert (
        second.censor_reason
        == "RECONCILED_NOT_FOUND_UNVERIFIED_ABSENCE_AUTHORITY"
    )
    assert second.reconciliation_external_effect_found is False


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



def test_attempt_projection_ignores_rebound_ledger_snapshot_seams(tmp_path):
    canonical = _ledger(
        tmp_path / "canonical-attempt",
        action=_action(selection_id="canonical-selection"),
    )
    _bind_provider(canonical)
    _ack(canonical)
    decoy = _ledger(
        tmp_path / "decoy-attempt",
        action=_action(selection_id="decoy-selection"),
    )
    _bind_provider(decoy)
    _ack(decoy)

    canonical_snapshot = RealExecutionLedger.verified_snapshot(canonical)
    decoy_snapshot = RealExecutionLedger.verified_snapshot(decoy)
    assert canonical_snapshot.sha256 != decoy_snapshot.sha256

    canonical.verified_snapshot = decoy.verified_snapshot
    canonical._parse = lambda _raw: RealExecutionLedger._parse(decoy.path.read_bytes())

    evidence = build_empirical_execution_evidence(
        canonical,
        attempt_id="attempt-1",
    )

    assert evidence.selection_id == "canonical-selection"
    assert evidence.source_ledger_sha256 == canonical_snapshot.sha256
    assert evidence.source_event_count == canonical_snapshot.event_count


def _population_ledger(tmp_path) -> RealExecutionLedger:
    ledger = RealExecutionLedger(tmp_path / "execution-population.jsonl")
    specs = (
        ("z", "ACCEPTED"),
        ("a", "REJECTED"),
        ("m", "UNKNOWN"),
        ("p", "PARTIAL"),
        ("r", "RESERVED"),
        ("s", "SUBMITTED"),
        ("n", "RECONCILED_NOT_FOUND"),
    )
    for index, (suffix, state) in enumerate(specs, start=1):
        action = replace(
            _action(),
            action_id=f"leg-{suffix}",
            selection_id=f"selection-{suffix}",
            quote_id=f"quote-{suffix}",
        )
        plan = ExecutionPlan(
            plan_id=f"plan-{suffix}",
            bookmaker_profile_version="profile-1",
            decision_id=f"decision-{suffix}",
            approval_id=f"approval-{suffix}",
            created_at=DECISION,
            actions=(action,),
        )
        attempt_id = f"attempt-{suffix}"
        ledger.reserve_plan(plan)
        ledger.begin_attempt(
            plan_id=plan.plan_id,
            action_id=action.action_id,
            attempt_id=attempt_id,
            reserved_at=RESERVED,
        )
        if state == "RESERVED":
            continue

        ledger.mark_submitted(attempt_id, submitted_at=SUBMITTED)
        if state == "SUBMITTED":
            continue
        if state == "UNKNOWN":
            ledger.mark_unknown(
                attempt_id,
                reason="provider timeout after submission",
                observed_at="2026-09-21T10:00:02+00:00",
            )
            continue
        if state == "RECONCILED_NOT_FOUND":
            ledger.mark_unknown(
                attempt_id,
                reason="provider timeout after submission",
                observed_at="2026-09-21T10:00:02+00:00",
            )
            ledger.reconcile_not_found(
                ReconciliationSnapshot(
                    attempt_id=attempt_id,
                    evidence_id=format(index, "064x"),
                    observed_at=RECONCILIATION_AT,
                    external_effect_found=False,
                    source=RECONCILIATION_SOURCE,
                )
            )
            continue

        ledger.bind_provider_evidence(
            attempt_id=attempt_id,
            evidence_id=format(index, "064x"),
            observed_at=PROVIDER,
            source="provider-response",
        )
        status = {
            "ACCEPTED": AcknowledgementStatus.ACCEPTED,
            "PARTIAL": AcknowledgementStatus.PARTIAL,
            "REJECTED": AcknowledgementStatus.REJECTED,
        }[state]
        ledger.acknowledge(
            ExternalAcknowledgement(
                attempt_id=attempt_id,
                external_receipt_id=f"receipt-{suffix}",
                status=status,
                acknowledged_at=ACKED,
                accepted_odds=(
                    None if status is AcknowledgementStatus.REJECTED else Decimal("2.08")
                ),
                accepted_stake=(
                    None
                    if status is AcknowledgementStatus.REJECTED
                    else (
                        Decimal("2.00")
                        if status is AcknowledgementStatus.PARTIAL
                        else Decimal("5.00")
                    )
                ),
            )
        )
    return ledger


def test_population_projection_ignores_rebound_ledger_snapshot_seams(tmp_path):
    canonical = _population_ledger(tmp_path / "canonical-population")
    decoy = _population_ledger(tmp_path / "decoy-population")

    canonical_snapshot = RealExecutionLedger.verified_snapshot(canonical)
    decoy_snapshot = RealExecutionLedger.verified_snapshot(decoy)
    assert canonical_snapshot.sha256 != decoy_snapshot.sha256

    canonical.verified_snapshot = decoy.verified_snapshot
    canonical._parse = lambda _raw: RealExecutionLedger._parse(decoy.path.read_bytes())

    aggregate = build_empirical_execution_population_evidence(
        canonical,
        evaluation_protocol_sha256="7" * 64,
    )

    assert aggregate.source_ledger_sha256 == canonical_snapshot.sha256
    assert aggregate.source_event_count == canonical_snapshot.event_count
    assert all(
        sample.source_ledger_sha256 == canonical_snapshot.sha256
        and sample.source_event_count == canonical_snapshot.event_count
        for sample in aggregate.samples
    )


def test_population_aggregate_keeps_complete_funnel_denominator(tmp_path):
    aggregate = build_empirical_execution_population_evidence(
        _population_ledger(tmp_path),
        evaluation_protocol_sha256="a" * 64,
    )

    assert isinstance(aggregate, EmpiricalExecutionPopulationEvidence)
    assert aggregate.total_attempts == 7
    assert aggregate.source_product_authority_verified is False
    assert (
        aggregate.source_root_authority_status
        == SOURCE_ROOT_AUTHORITY_UNQUALIFIED
    )
    assert aggregate.evaluation_protocol_authority_verified is False
    assert (
        aggregate.evaluation_protocol_authority_status
        == EVALUATION_PROTOCOL_AUTHORITY_UNQUALIFIED
    )
    assert all(
        sample.source_product_authority_verified is False
        and sample.source_root_authority_status == SOURCE_ROOT_AUTHORITY_UNQUALIFIED
        for sample in aggregate.samples
    )
    assert tuple(sample.attempt_id for sample in aggregate.samples) == tuple(
        sorted(sample.attempt_id for sample in aggregate.samples)
    )
    assert dict(aggregate.state_counts) == {
        "RESERVED": 1,
        "SUBMITTED": 1,
        "UNKNOWN": 1,
        "ACCEPTED": 1,
        "PARTIAL": 1,
        "REJECTED": 1,
        "RECONCILED_NOT_FOUND": 1,
    }
    assert aggregate.ledger_terminal_count == 3
    assert aggregate.provider_verified_terminal_count == 0
    assert aggregate.unverified_ledger_terminal_count == 3
    assert aggregate.right_censored_count == 4
    assert aggregate.provider_evidence_count == 3
    assert aggregate.provider_outcome_unverified_ack_count == 3
    assert aggregate.provider_outcome_unverified_absence_count == 1
    assert aggregate.provider_outcome_not_applicable_count == 3
    assert aggregate.slippage_known_count == 0
    assert aggregate.slippage_unknown_count == 6
    assert aggregate.slippage_not_applicable_count == 1
    assert aggregate.causal_timing_known_count == 0
    assert aggregate.causal_timing_unknown_count == 7

    payload = aggregate.to_dict()
    assert payload["source_product_authority_verified"] is False
    assert (
        payload["source_root_authority_status"]
        == SOURCE_ROOT_AUTHORITY_UNQUALIFIED
    )
    assert payload["evaluation_protocol_authority_verified"] is False
    assert (
        payload["evaluation_protocol_authority_status"]
        == EVALUATION_PROTOCOL_AUTHORITY_UNQUALIFIED
    )
    assert payload["state_rates"]["ACCEPTED"] == {
        "numerator": 1,
        "denominator": 7,
    }
    assert payload["ledger_terminal_rate"] == {
        "numerator": 3,
        "denominator": 7,
    }
    assert payload["provider_verified_terminal_rate"] == {
        "numerator": 0,
        "denominator": 7,
    }
    assert payload["unverified_ledger_terminal_rate"] == {
        "numerator": 3,
        "denominator": 7,
    }
    assert payload["provider_outcome_verification_counts"] == {
        PROVIDER_OUTCOME_UNVERIFIED_ACK: 3,
        PROVIDER_OUTCOME_UNVERIFIED_ABSENCE: 1,
        PROVIDER_OUTCOME_NOT_APPLICABLE: 3,
    }
    assert payload["right_censored_rate"] == {
        "numerator": 4,
        "denominator": 7,
    }
    assert payload["provider_evidence_rate"] == {
        "numerator": 3,
        "denominator": 7,
    }


def test_population_aggregate_cannot_hide_rejected_or_unknown_samples(tmp_path):
    aggregate = build_empirical_execution_population_evidence(
        _population_ledger(tmp_path),
        evaluation_protocol_sha256="b" * 64,
    )

    states = [sample.attempt_state for sample in aggregate.samples]
    assert "ACCEPTED" in states
    assert "REJECTED" in states
    assert "UNKNOWN" in states
    assert aggregate.total_attempts == len(states)
    assert sum(dict(aggregate.state_counts).values()) == aggregate.total_attempts


def test_population_aggregate_is_restart_deterministic(tmp_path):
    ledger = _population_ledger(tmp_path)
    first = build_empirical_execution_population_evidence(
        ledger,
        evaluation_protocol_sha256="c" * 64,
    )

    restarted = RealExecutionLedger(ledger.path)
    second = build_empirical_execution_population_evidence(
        restarted,
        evaluation_protocol_sha256="c" * 64,
    )

    assert second.to_dict() == first.to_dict()
    assert second.denominator_sha256 == first.denominator_sha256
    assert second.evidence_sha256 == first.evidence_sha256


def test_population_aggregate_rejects_noncanonical_protocol_digest(tmp_path):
    with pytest.raises(
        EmpiricalExecutionEvidenceError,
        match="evaluation_protocol_sha256",
    ):
        build_empirical_execution_population_evidence(
            _population_ledger(tmp_path),
            evaluation_protocol_sha256="caller-label",
        )




def test_population_protocol_digest_is_explicitly_non_authoritative(tmp_path):
    aggregate = build_empirical_execution_population_evidence(
        _population_ledger(tmp_path),
        evaluation_protocol_sha256="9" * 64,
    )

    payload = aggregate.to_dict()
    assert payload["evaluation_protocol_sha256"] == "9" * 64
    assert payload["evaluation_protocol_authority_verified"] is False
    assert (
        payload["evaluation_protocol_authority_status"]
        == EVALUATION_PROTOCOL_AUTHORITY_UNQUALIFIED
    )

    object.__setattr__(
        aggregate,
        "evaluation_protocol_authority_verified",
        True,
    )
    with pytest.raises(
        EmpiricalExecutionEvidenceError,
        match="not issued by whole-ledger projection",
    ):
        aggregate.to_dict()


def test_population_protocol_authority_status_mutation_revokes_issuance(tmp_path):
    aggregate = build_empirical_execution_population_evidence(
        _population_ledger(tmp_path),
        evaluation_protocol_sha256="8" * 64,
    )

    object.__setattr__(
        aggregate,
        "evaluation_protocol_authority_status",
        "PRODUCT_PROTOCOL_VERIFIED",
    )
    with pytest.raises(
        EmpiricalExecutionEvidenceError,
        match="not issued by whole-ledger projection",
    ):
        _ = aggregate.evidence_sha256

def test_population_evidence_cannot_be_caller_constructed_or_subset_with_replace(
    tmp_path,
):
    aggregate = build_empirical_execution_population_evidence(
        _population_ledger(tmp_path),
        evaluation_protocol_sha256="d" * 64,
    )

    with pytest.raises(TypeError):
        EmpiricalExecutionPopulationEvidence(
            source_ledger_sha256=aggregate.source_ledger_sha256,
            source_event_count=aggregate.source_event_count,
            evaluation_protocol_sha256=aggregate.evaluation_protocol_sha256,
            samples=aggregate.samples[:1],
        )

    with pytest.raises(TypeError):
        replace(aggregate, samples=aggregate.samples[:1])



def test_population_same_object_mutation_revokes_whole_ledger_issuance(tmp_path):
    aggregate = build_empirical_execution_population_evidence(
        _population_ledger(tmp_path),
        evaluation_protocol_sha256="e" * 64,
    )
    original_hash = aggregate.evidence_sha256

    object.__setattr__(
        aggregate,
        "evaluation_protocol_sha256",
        "f" * 64,
    )

    assert aggregate._evidence_sha256 == original_hash
    with pytest.raises(
        EmpiricalExecutionEvidenceError,
        match="not issued by whole-ledger projection",
    ):
        aggregate.to_dict()


def test_population_copy_cannot_inherit_whole_ledger_issuance(tmp_path):
    aggregate = build_empirical_execution_population_evidence(
        _population_ledger(tmp_path),
        evaluation_protocol_sha256="1" * 64,
    )
    duplicated = copy(aggregate)

    assert duplicated is not aggregate
    with pytest.raises(
        EmpiricalExecutionEvidenceError,
        match="not issued by whole-ledger projection",
    ):
        _ = duplicated.evidence_sha256
