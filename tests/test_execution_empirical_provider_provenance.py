from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from decimal import Decimal

import pytest

from autosport.execution_empirical_evidence import (
    EmpiricalExecutionEvidenceError,
    PROVIDER_OUTCOME_NOT_APPLICABLE,
    PROVIDER_OUTCOME_UNVERIFIED_ACK,
    SLIPPAGE_STATUS_UNKNOWN,
    build_empirical_execution_evidence,
    build_empirical_execution_population_evidence,
)
from autosport.real_execution_ledger import (
    AcknowledgementStatus,
    ExecutionAction,
    ExecutionPlan,
    ExternalAcknowledgement,
    RealExecutionLedger,
)


QUOTE = "2026-10-03T08:00:00+00:00"
DECISION = "2026-10-03T08:00:01+00:00"
RESERVED = "2026-10-03T08:00:01.100000+00:00"
SUBMITTED = "2026-10-03T08:00:01.200000+00:00"
ACKED = "2026-10-03T08:00:01.700000+00:00"
REQUEST_SHA256 = "a" * 64
EVIDENCE_ID = "e" * 64


def _canonical_digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _ledger(tmp_path, *, request_sha256: str | None = REQUEST_SHA256) -> RealExecutionLedger:
    ledger = RealExecutionLedger(tmp_path / "execution.jsonl")
    action = ExecutionAction(
        action_id="leg-1",
        bookmaker_id="betfair",
        account_id="account-1",
        event_id="event-1",
        market_id="market-1",
        selection_id="selection-1",
        side="BACK",
        requested_odds=Decimal("2.10"),
        requested_stake=Decimal("5.00"),
        quote_id="quote-1",
        quote_observed_at=QUOTE,
        expires_at="2026-10-03T08:01:00+00:00",
    )
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
        plan_id=plan.plan_id,
        action_id=action.action_id,
        attempt_id="attempt-1",
        reserved_at=RESERVED,
    )
    ledger.mark_submitted(
        "attempt-1",
        submitted_at=SUBMITTED,
        request_sha256=request_sha256,
    )
    return ledger


def _acknowledgement() -> ExternalAcknowledgement:
    return ExternalAcknowledgement(
        attempt_id="attempt-1",
        external_receipt_id="receipt-1",
        status=AcknowledgementStatus.ACCEPTED,
        acknowledged_at=ACKED,
        accepted_odds=Decimal("2.08"),
        accepted_stake=Decimal("5.00"),
    )


def _bind_exact_ack(
    ledger: RealExecutionLedger,
    acknowledgement: ExternalAcknowledgement,
) -> str:
    ledger._bind_provider_acknowledgement_evidence(
        attempt_id="attempt-1",
        evidence_id=EVIDENCE_ID,
        observed_at=ACKED,
        source="betfair-place-orders-response",
        request_sha256=REQUEST_SHA256,
        acknowledgement=acknowledgement,
    )
    return _canonical_digest(acknowledgement.to_dict())


def test_exact_provider_request_and_ack_digests_are_projected_without_promotion(
    tmp_path,
):
    ledger = _ledger(tmp_path)
    acknowledgement = _acknowledgement()
    acknowledgement_sha256 = _bind_exact_ack(ledger, acknowledgement)
    ledger.acknowledge(acknowledgement)

    evidence = build_empirical_execution_evidence(
        ledger,
        attempt_id="attempt-1",
    )
    payload = evidence.to_dict()

    assert evidence.submitted_request_sha256 == REQUEST_SHA256
    assert evidence.provider_evidence_request_sha256 == REQUEST_SHA256
    assert (
        evidence.provider_evidence_acknowledgement_sha256
        == acknowledgement_sha256
    )
    assert evidence.acknowledgement_payload_sha256 == acknowledgement_sha256

    assert payload["schema_version"] == 6
    assert payload["submitted_request_sha256"] == REQUEST_SHA256
    assert payload["provider_evidence_request_sha256"] == REQUEST_SHA256
    assert (
        payload["provider_evidence_acknowledgement_sha256"]
        == acknowledgement_sha256
    )
    assert payload["acknowledgement_payload_sha256"] == acknowledgement_sha256

    # Provenance projection is not root/provider qualification.
    assert evidence.source_product_authority_verified is False
    assert evidence.provider_outcome_verified is False
    assert (
        evidence.provider_outcome_verification_reason
        == PROVIDER_OUTCOME_UNVERIFIED_ACK
    )
    assert evidence.slippage_status == SLIPPAGE_STATUS_UNKNOWN
    assert evidence.accepted_odds is None
    assert evidence.accepted_stake is None


def test_bound_provider_ack_before_terminal_ledger_transition_stays_nonterminal(
    tmp_path,
):
    ledger = _ledger(tmp_path)
    acknowledgement = _acknowledgement()
    acknowledgement_sha256 = _bind_exact_ack(ledger, acknowledgement)

    evidence = build_empirical_execution_evidence(
        ledger,
        attempt_id="attempt-1",
    )

    assert evidence.attempt_state == "SUBMITTED"
    assert evidence.right_censored is True
    assert evidence.provider_outcome_verified is False
    assert (
        evidence.provider_outcome_verification_reason
        == PROVIDER_OUTCOME_NOT_APPLICABLE
    )
    assert evidence.submitted_request_sha256 == REQUEST_SHA256
    assert evidence.provider_evidence_request_sha256 == REQUEST_SHA256
    assert (
        evidence.provider_evidence_acknowledgement_sha256
        == acknowledgement_sha256
    )
    assert evidence.acknowledgement_payload_sha256 is None
    assert evidence.slippage_status == SLIPPAGE_STATUS_UNKNOWN


def test_generic_provider_request_binding_is_projected_without_ack_authority(
    tmp_path,
):
    ledger = _ledger(tmp_path)
    ledger.bind_provider_evidence(
        attempt_id="attempt-1",
        evidence_id=EVIDENCE_ID,
        observed_at=ACKED,
        source="provider-readback",
        request_sha256=REQUEST_SHA256,
    )

    evidence = build_empirical_execution_evidence(
        ledger,
        attempt_id="attempt-1",
    )

    assert evidence.submitted_request_sha256 == REQUEST_SHA256
    assert evidence.provider_evidence_request_sha256 == REQUEST_SHA256
    assert evidence.provider_evidence_acknowledgement_sha256 is None
    assert evidence.acknowledgement_payload_sha256 is None
    assert evidence.provider_outcome_verified is False
    assert evidence.slippage_status == SLIPPAGE_STATUS_UNKNOWN


def test_legacy_submission_remains_projectable_without_invented_request_identity(
    tmp_path,
):
    ledger = _ledger(tmp_path, request_sha256=None)
    ledger.bind_provider_evidence(
        attempt_id="attempt-1",
        evidence_id=EVIDENCE_ID,
        observed_at=ACKED,
        source="legacy-provider-readback",
    )

    evidence = build_empirical_execution_evidence(
        ledger,
        attempt_id="attempt-1",
    )

    assert evidence.submitted_request_sha256 is None
    assert evidence.provider_evidence_request_sha256 is None
    assert evidence.provider_evidence_acknowledgement_sha256 is None
    assert evidence.acknowledgement_payload_sha256 is None


def test_projection_rejects_caller_forged_provider_request_digest(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.bind_provider_evidence(
        attempt_id="attempt-1",
        evidence_id=EVIDENCE_ID,
        observed_at=ACKED,
        source="provider-readback",
        request_sha256=REQUEST_SHA256,
    )
    evidence = build_empirical_execution_evidence(
        ledger,
        attempt_id="attempt-1",
    )

    with pytest.raises(
        EmpiricalExecutionEvidenceError,
        match="provider request provenance mismatches durable submission",
    ):
        replace(
            evidence,
            provider_evidence_request_sha256="b" * 64,
        )


def test_projection_rejects_caller_forged_provider_ack_digest(tmp_path):
    ledger = _ledger(tmp_path)
    acknowledgement = _acknowledgement()
    _bind_exact_ack(ledger, acknowledgement)
    ledger.acknowledge(acknowledgement)
    evidence = build_empirical_execution_evidence(
        ledger,
        attempt_id="attempt-1",
    )

    with pytest.raises(
        EmpiricalExecutionEvidenceError,
        match="provider acknowledgement provenance mismatches durable acknowledgement",
    ):
        replace(
            evidence,
            provider_evidence_acknowledgement_sha256="b" * 64,
        )

    with pytest.raises(
        EmpiricalExecutionEvidenceError,
        match="durable acknowledgement requires canonical payload identity",
    ):
        replace(
            evidence,
            acknowledgement_payload_sha256=None,
        )


def test_population_evidence_retains_provider_provenance_in_sample_hashes(tmp_path):
    ledger = _ledger(tmp_path)
    acknowledgement = _acknowledgement()
    acknowledgement_sha256 = _bind_exact_ack(ledger, acknowledgement)
    ledger.acknowledge(acknowledgement)

    population = build_empirical_execution_population_evidence(
        ledger,
        evaluation_protocol_sha256="f" * 64,
    )
    payload = population.to_dict()

    assert payload["schema_version"] == 8
    assert len(population.samples) == 1
    sample = population.samples[0]
    assert sample.submitted_request_sha256 == REQUEST_SHA256
    assert (
        sample.provider_evidence_acknowledgement_sha256
        == acknowledgement_sha256
    )
    assert sample.provider_outcome_verified is False
    assert population.submitted_request_identity_count == 1
    assert population.provider_request_binding_count == 1
    assert population.provider_acknowledgement_binding_count == 1
    assert population.durable_acknowledgement_identity_count == 1
    assert population.provider_bound_durable_ack_count == 1
    assert payload["provider_bound_durable_ack_rate"] == {
        "numerator": 1,
        "denominator": 1,
    }


def test_new_provenance_fields_participate_in_projection_issuance_digest(tmp_path):
    ledger = _ledger(tmp_path)
    evidence = build_empirical_execution_evidence(
        ledger,
        attempt_id="attempt-1",
    )
    original = evidence.evidence_sha256

    object.__setattr__(
        evidence,
        "submitted_request_sha256",
        "b" * 64,
    )

    assert evidence._evidence_sha256 == original
    with pytest.raises(
        EmpiricalExecutionEvidenceError,
        match="not issued by canonical ledger projection",
    ):
        evidence.to_dict()
