from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

import autosport.execution_quote_chain as execution_quote_chain
import autosport.real_execution_ledger as real_execution_ledger

from autosport.execution_quote_chain import (
    ACCEPTED_PRICE_ACKNOWLEDGED_UNVERIFIED,
    ACCEPTED_PRICE_NOT_APPLICABLE,
    ACCEPTED_PRICE_PROVIDER_VERIFIED,
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
REQUEST_SHA256 = "a" * 64
SOURCE_PAYLOAD_SHA256 = "b" * 64


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


def _submitted(
    tmp_path,
    *,
    action: ExecutionAction | None = None,
    request_sha256: str | None = None,
) -> RealExecutionLedger:
    ledger = _reserved(tmp_path, action=action)
    ledger.mark_submitted(
        "attempt-1",
        submitted_at=SUBMITTED,
        request_sha256=request_sha256,
    )
    return ledger


def _bind_provider(
    ledger: RealExecutionLedger,
    *,
    request_sha256: str | None = None,
) -> None:
    ledger.bind_provider_evidence(
        attempt_id="attempt-1",
        evidence_id=EVIDENCE_ID,
        observed_at=PROVIDER,
        source="provider-response",
        request_sha256=request_sha256,
    )


def _ack(
    ledger: RealExecutionLedger,
    *,
    status: AcknowledgementStatus = AcknowledgementStatus.ACCEPTED,
    odds: Decimal | None = Decimal("2.08"),
    stake: Decimal | None = Decimal("10.00"),
    request_sha256: str | None = None,
) -> None:
    acknowledgement = ExternalAcknowledgement(
        attempt_id="attempt-1",
        external_receipt_id="receipt-1",
        status=status,
        acknowledged_at=ACKED,
        accepted_odds=odds,
        accepted_stake=stake,
    )
    ledger._bind_provider_acknowledgement_evidence(
        attempt_id="attempt-1",
        evidence_id=EVIDENCE_ID,
        observed_at=ACKED,
        source="provider-response",
        request_sha256=request_sha256,
        acknowledgement=acknowledgement,
    )
    ledger.acknowledge(acknowledgement)


def _append_verified_provider_effect_fact(
    ledger: RealExecutionLedger,
    *,
    status: AcknowledgementStatus = AcknowledgementStatus.ACCEPTED,
    odds: Decimal = Decimal("2.08"),
    stake: Decimal = Decimal("10.00"),
) -> None:
    """Model an already-issued provider-origin fact for read-only projection tests."""

    payload = real_execution_ledger.VerifiedProviderEffectBindingView(
        evidence_id=EVIDENCE_ID,
        observed_at=ACKED,
        source_payload_sha256=SOURCE_PAYLOAD_SHA256,
        external_receipt_id="receipt-1",
        status=status,
        accepted_odds=odds,
        accepted_stake=stake,
        provider_order_ref=None,
    ).to_dict()
    ledger._append(
        real_execution_ledger.EventType.VERIFIED_PROVIDER_EFFECT_BOUND,
        "plan-1",
        "action-1",
        "attempt-1",
        payload,
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


def test_exact_request_digest_binds_submit_to_provider_without_promoting_price(
    tmp_path,
) -> None:
    ledger = _submitted(tmp_path, request_sha256=REQUEST_SHA256)
    _bind_provider(ledger, request_sha256=REQUEST_SHA256)

    evidence = _project(ledger)

    assert evidence.chain_status == CHAIN_SUBMIT_INSTRUCTION_BOUND
    assert evidence.submission_instruction_sha256 == REQUEST_SHA256
    assert evidence.provider_request_sha256 == REQUEST_SHA256
    assert evidence.submit_instruction_identity_bound is True
    assert evidence.provider_request_correlation_bound is True
    assert evidence.actual_submitted_instruction_bound is True
    assert evidence.accepted_price_status == ACCEPTED_PRICE_UNKNOWN
    assert evidence.accepted_price_verified is False
    assert evidence.chain_complete is False


def test_exact_provider_ack_binding_is_visible_without_promoting_price_authority(
    tmp_path,
) -> None:
    ledger = _submitted(tmp_path, request_sha256=REQUEST_SHA256)
    _ack(
        ledger,
        odds=Decimal("2.08"),
        stake=Decimal("10.00"),
        request_sha256=REQUEST_SHA256,
    )

    evidence = _project(ledger)

    assert evidence.chain_status == CHAIN_SUBMIT_INSTRUCTION_BOUND
    assert evidence.actual_submitted_instruction_bound is True
    assert evidence.acknowledgement_binding_matches is True
    assert evidence.accepted_price_status == ACCEPTED_PRICE_ACKNOWLEDGED_UNVERIFIED
    assert evidence.accepted_price_verified is False
    assert evidence.chain_complete is False


def test_verified_provider_outcome_completes_only_exact_request_chain(
    tmp_path,
) -> None:
    ledger = _submitted(tmp_path, request_sha256=REQUEST_SHA256)
    _ack(
        ledger,
        odds=Decimal("2.08"),
        stake=Decimal("10.00"),
        request_sha256=REQUEST_SHA256,
    )
    _append_verified_provider_effect_fact(ledger)

    evidence = _project(ledger)

    assert evidence.provider_outcome_evidence_id == EVIDENCE_ID
    assert evidence.provider_outcome_source_payload_sha256 == SOURCE_PAYLOAD_SHA256
    assert evidence.provider_outcome_status == "ACCEPTED"
    assert evidence.provider_accepted_odds == Decimal("2.08")
    assert evidence.provider_accepted_stake == Decimal("10.00")
    assert evidence.accepted_price_status == ACCEPTED_PRICE_PROVIDER_VERIFIED
    assert evidence.accepted_price_verified is True
    assert evidence.actual_submitted_instruction_bound is True
    assert evidence.chain_complete is True


def test_verified_provider_outcome_without_request_correlation_stays_incomplete(
    tmp_path,
) -> None:
    ledger = _submitted(tmp_path)
    _ack(
        ledger,
        odds=Decimal("2.08"),
        stake=Decimal("10.00"),
    )
    _append_verified_provider_effect_fact(ledger)

    evidence = _project(ledger)

    assert evidence.accepted_price_status == ACCEPTED_PRICE_PROVIDER_VERIFIED
    assert evidence.accepted_price_verified is True
    assert evidence.actual_submitted_instruction_bound is False
    assert evidence.chain_complete is False


def test_verified_provider_outcome_and_chain_completion_survive_restart(
    tmp_path,
) -> None:
    ledger = _submitted(tmp_path, request_sha256=REQUEST_SHA256)
    _ack(ledger, request_sha256=REQUEST_SHA256)
    _append_verified_provider_effect_fact(ledger)

    first = _project(ledger)
    reopened = RealExecutionLedger(ledger.path)
    second = _project(reopened)

    assert first.accepted_price_verified is True
    assert first.chain_complete is True
    assert second.accepted_price_verified is True
    assert second.chain_complete is True
    assert second.to_dict() == first.to_dict()
    assert second.evidence_sha256 == first.evidence_sha256


def test_exact_request_and_provider_ack_binding_survive_restart(
    tmp_path,
) -> None:
    ledger = _submitted(tmp_path, request_sha256=REQUEST_SHA256)
    _ack(
        ledger,
        request_sha256=REQUEST_SHA256,
    )
    first = _project(ledger)

    reopened = RealExecutionLedger(ledger.path)
    second = _project(reopened)

    assert first.actual_submitted_instruction_bound is True
    assert first.acknowledgement_binding_matches is True
    assert second.actual_submitted_instruction_bound is True
    assert second.acknowledgement_binding_matches is True
    assert second.accepted_price_verified is False
    assert second.chain_complete is False
    assert second.to_dict() == first.to_dict()
    assert second.evidence_sha256 == first.evidence_sha256


def test_crash_after_provider_ack_evidence_before_terminal_ack_stays_uncertain(
    tmp_path,
) -> None:
    ledger = _submitted(tmp_path, request_sha256=REQUEST_SHA256)
    acknowledgement = ExternalAcknowledgement(
        attempt_id="attempt-1",
        external_receipt_id="receipt-1",
        status=AcknowledgementStatus.ACCEPTED,
        acknowledged_at=ACKED,
        accepted_odds=Decimal("2.08"),
        accepted_stake=Decimal("10.00"),
    )
    ledger._bind_provider_acknowledgement_evidence(
        attempt_id="attempt-1",
        evidence_id=EVIDENCE_ID,
        observed_at=ACKED,
        source="provider-response",
        request_sha256=REQUEST_SHA256,
        acknowledgement=acknowledgement,
    )

    restarted = RealExecutionLedger(ledger.path)
    assert restarted.recover_uncertain() == ("attempt-1",)

    evidence = _project(restarted)

    assert evidence.attempt_state == "UNKNOWN"
    assert evidence.chain_status == CHAIN_SUBMIT_INSTRUCTION_BOUND
    assert evidence.actual_submitted_instruction_bound is True
    assert evidence.provider_acknowledgement_sha256 is not None
    assert evidence.acknowledgement_status is None
    assert evidence.acknowledgement_binding_matches is False
    assert evidence.accepted_price_status == ACCEPTED_PRICE_UNKNOWN
    assert evidence.accepted_price_verified is False
    assert evidence.chain_complete is False


def test_numeric_equal_acknowledgement_still_cannot_complete_chain(tmp_path) -> None:
    ledger = _submitted(tmp_path)
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


def test_replaced_verified_execution_view_cannot_mint_positive_request_binding(
    tmp_path,
    monkeypatch,
) -> None:
    ledger = _submitted(tmp_path)
    _bind_provider(ledger)

    def hostile_view(self, plan_id):
        raise AssertionError("hostile verified_execution_view executed")

    monkeypatch.setattr(
        RealExecutionLedger,
        "verified_execution_view",
        hostile_view,
    )

    with pytest.raises(
        ExecutionQuoteChainUnavailable,
        match="canonical verified execution view authority is unavailable",
    ):
        _project(ledger)


def test_instance_shadow_verified_execution_view_fails_closed(
    tmp_path,
    monkeypatch,
) -> None:
    ledger = _submitted(tmp_path)

    def hostile_view(plan_id):
        raise AssertionError("instance-shadowed verified_execution_view executed")

    monkeypatch.setattr(
        ledger,
        "verified_execution_view",
        hostile_view,
    )

    with pytest.raises(
        ExecutionQuoteChainUnavailable,
        match="canonical verified execution view authority is unavailable",
    ):
        _project(ledger)


def test_in_place_verified_execution_view_code_replacement_fails_closed(
    tmp_path,
    monkeypatch,
) -> None:
    ledger = _submitted(tmp_path)

    def hostile_view(self, plan_id):
        raise AssertionError("hostile verified_execution_view code executed")

    monkeypatch.setattr(
        RealExecutionLedger.verified_execution_view,
        "__code__",
        hostile_view.__code__,
    )

    with pytest.raises(
        ExecutionQuoteChainUnavailable,
        match="canonical verified execution view authority is unavailable",
    ):
        _project(ledger)


def test_evidence_invariant_rejects_conflicting_future_request_digests(
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


def test_quote_chain_class_seal_witnesses_are_not_mutable_metaclass_attributes() -> None:
    evidence_type = execution_quote_chain.ExecutionQuoteChainEvidence
    metaclass = type(evidence_type)

    assert "_sealed_classes" not in metaclass.__dict__
    assert "_protected_names" not in metaclass.__dict__


def test_quote_chain_authority_surfaces_reject_runtime_class_rebinding() -> None:
    evidence_type = execution_quote_chain.ExecutionQuoteChainEvidence

    for name in (
        "assert_projection_issued",
        "submit_instruction_identity_bound",
        "provider_request_correlation_bound",
        "actual_submitted_instruction_bound",
        "acknowledgement_binding_matches",
        "accepted_price_verified",
        "chain_complete",
        "evidence_sha256",
        "to_dict",
    ):
        original = evidence_type.__dict__[name]
        with pytest.raises(
            TypeError,
            match="quote-chain evidence authority surface is sealed",
        ):
            setattr(evidence_type, name, object())
        assert evidence_type.__dict__[name] is original
        with pytest.raises(
            TypeError,
            match="quote-chain evidence authority surface is sealed",
        ):
            delattr(evidence_type, name)
        assert evidence_type.__dict__[name] is original


def test_quote_chain_builder_rejects_module_class_alias_rebinding(
    tmp_path,
    monkeypatch,
) -> None:
    ledger = _submitted(tmp_path)

    class ForgedEvidence:
        pass

    monkeypatch.setattr(
        execution_quote_chain,
        "ExecutionQuoteChainEvidence",
        ForgedEvidence,
    )

    with pytest.raises(
        ExecutionQuoteChainError,
        match="quote-chain evidence class authority is unavailable",
    ):
        _project(ledger)


def test_quote_chain_mint_state_is_not_importable_from_module_namespace() -> None:
    assert not hasattr(execution_quote_chain, "_QUOTE_CHAIN_EVIDENCE_ISSUANCE_TOKEN")
    assert not hasattr(execution_quote_chain, "_ISSUED_QUOTE_CHAIN_EVIDENCE")
    assert not hasattr(execution_quote_chain, "_register_issued_quote_chain_evidence")


def test_reconstructed_quote_chain_cannot_mint_submit_binding(tmp_path) -> None:
    canonical = _project(_submitted(tmp_path))
    forged = replace(
        canonical,
        submission_instruction_sha256="a" * 64,
        provider_evidence_id="b" * 64,
        provider_evidence_observed_at=PROVIDER,
        provider_evidence_source="caller-forged-provider-evidence",
        provider_request_sha256="a" * 64,
        chain_status=CHAIN_SUBMIT_INSTRUCTION_BOUND,
    )

    for authority_name in (
        "submit_instruction_identity_bound",
        "provider_request_correlation_bound",
        "actual_submitted_instruction_bound",
    ):
        with pytest.raises(
            ExecutionQuoteChainError,
            match="not issued by canonical ledger projection",
        ):
            getattr(forged, authority_name)

    with pytest.raises(
        ExecutionQuoteChainError,
        match="not issued by canonical ledger projection",
    ):
        _ = forged.evidence_sha256


def test_issued_quote_chain_mutation_cannot_mint_submit_binding(tmp_path) -> None:
    evidence = _project(_submitted(tmp_path))

    object.__setattr__(
        evidence,
        "submission_instruction_sha256",
        "a" * 64,
    )
    object.__setattr__(
        evidence,
        "provider_evidence_id",
        "b" * 64,
    )
    object.__setattr__(
        evidence,
        "provider_evidence_observed_at",
        PROVIDER,
    )
    object.__setattr__(
        evidence,
        "provider_evidence_source",
        "caller-forged-provider-evidence",
    )
    object.__setattr__(
        evidence,
        "provider_request_sha256",
        "a" * 64,
    )
    object.__setattr__(
        evidence,
        "chain_status",
        CHAIN_SUBMIT_INSTRUCTION_BOUND,
    )

    for authority_name in (
        "submit_instruction_identity_bound",
        "provider_request_correlation_bound",
        "actual_submitted_instruction_bound",
    ):
        with pytest.raises(
            ExecutionQuoteChainError,
            match="not issued by canonical ledger projection",
        ):
            getattr(evidence, authority_name)

    with pytest.raises(
        ExecutionQuoteChainError,
        match="not issued by canonical ledger projection",
    ):
        evidence.to_dict()

    with pytest.raises(
        ExecutionQuoteChainError,
        match="not issued by canonical ledger projection",
    ):
        _ = evidence.evidence_sha256


def test_quote_chain_builder_rejects_projection_read_helper_rebinding(
    tmp_path,
    monkeypatch,
) -> None:
    ledger = _submitted(tmp_path)

    def hostile_read(ledger, plan_id):
        raise AssertionError("hostile projection read helper executed")

    monkeypatch.setattr(
        execution_quote_chain,
        "_read_canonical_verified_execution_view",
        hostile_read,
    )

    with pytest.raises(
        ExecutionQuoteChainUnavailable,
        match="projection dependency authority is unavailable",
    ):
        _project(ledger)


def test_quote_chain_builder_rejects_projection_digest_rebinding(
    tmp_path,
    monkeypatch,
) -> None:
    ledger = _submitted(tmp_path)

    def hostile_digest(payload):
        raise AssertionError("hostile digest helper executed")

    monkeypatch.setattr(
        execution_quote_chain,
        "_digest",
        hostile_digest,
    )

    with pytest.raises(
        ExecutionQuoteChainUnavailable,
        match="projection dependency authority is unavailable",
    ):
        _project(ledger)


def test_quote_chain_builder_rejects_in_place_projection_helper_code_rebinding(
    tmp_path,
    monkeypatch,
) -> None:
    ledger = _submitted(tmp_path)
    helper = execution_quote_chain._read_canonical_verified_execution_view

    def hostile_read(ledger, plan_id):
        raise AssertionError("hostile projection read helper code executed")

    monkeypatch.setattr(helper, "__code__", hostile_read.__code__)

    with pytest.raises(
        ExecutionQuoteChainUnavailable,
        match="projection dependency authority is unavailable",
    ):
        _project(ledger)


def test_quote_chain_evidence_constructor_and_field_descriptors_are_sealed() -> None:
    evidence_type = execution_quote_chain.ExecutionQuoteChainEvidence

    for name in (
        "__init__",
        "__post_init__",
        "submission_instruction_sha256",
        "provider_request_sha256",
        "acknowledgement_binding_matches",
        "_evidence_sha256",
    ):
        original = evidence_type.__dict__[name]
        with pytest.raises(
            TypeError,
            match="quote-chain evidence authority surface is sealed",
        ):
            setattr(evidence_type, name, object())
        assert evidence_type.__dict__[name] is original


def test_issued_quote_chain_mutation_cannot_mint_acknowledgement_binding(
    tmp_path,
) -> None:
    evidence = _project(_submitted(tmp_path))

    assert evidence.acknowledgement_binding_matches is False
    object.__setattr__(
        evidence,
        "_acknowledgement_binding_matches",
        True,
    )

    with pytest.raises(
        ExecutionQuoteChainError,
        match="not issued by canonical ledger projection",
    ):
        _ = evidence.acknowledgement_binding_matches



def test_to_dict_returns_detached_canonical_issuance_snapshot(tmp_path) -> None:
    evidence = _project(_submitted(tmp_path))
    first = evidence.to_dict()

    first["actual_submitted_instruction_bound"] = True
    first["accepted_price_verified"] = True
    first["chain_complete"] = True
    first["submission_instruction_sha256"] = "f" * 64

    second = evidence.to_dict()

    assert second["actual_submitted_instruction_bound"] is False
    assert second["accepted_price_verified"] is False
    assert second["chain_complete"] is False
    assert second["submission_instruction_sha256"] is None
    assert second["evidence_sha256"] == evidence.evidence_sha256


def test_hard_false_quote_chain_authority_getters_have_no_mutable_python_code() -> None:
    evidence_type = execution_quote_chain.ExecutionQuoteChainEvidence

    for name in ("accepted_price_verified", "chain_complete"):
        descriptor = evidence_type.__dict__[name]
        getter = descriptor.fget
        assert getter is not None
        assert not hasattr(getter, "__code__")

        def hostile_getter(_self):
            return True

        with pytest.raises((AttributeError, TypeError)):
            setattr(getter, "__code__", hostile_getter.__code__)


def test_hard_false_quote_chain_authority_constants_are_class_sealed(
    tmp_path,
) -> None:
    evidence_type = execution_quote_chain.ExecutionQuoteChainEvidence
    evidence = _project(_submitted(tmp_path))

    assert evidence.accepted_price_verified is False
    assert evidence.chain_complete is False

    for name in (
        "_accepted_price_verified_constant",
        "_chain_complete_constant",
    ):
        with pytest.raises(
            TypeError,
            match="quote-chain evidence authority surface is sealed",
        ):
            setattr(evidence_type, name, True)

        with pytest.raises((AttributeError, TypeError)):
            object.__setattr__(evidence, name, True)

    assert evidence.accepted_price_verified is False
    assert evidence.chain_complete is False
    payload = evidence.to_dict()
    assert payload["accepted_price_verified"] is False
    assert payload["chain_complete"] is False


@pytest.mark.parametrize(
    "upstream_type_name",
    (
        "VerifiedExecutionPlanView",
        "ExecutionAttemptReadView",
        "ProviderEvidenceBindingView",
        "ExecutionAttempt",
        "ExecutionAction",
        "ExternalAcknowledgement",
        "ExecutionPlan",
    ),
)
def test_quote_chain_rejects_upstream_verified_view_type_alias_rebinding(
    tmp_path,
    monkeypatch,
    upstream_type_name: str,
) -> None:
    ledger = _submitted(tmp_path, request_sha256=REQUEST_SHA256)
    monkeypatch.setattr(
        real_execution_ledger,
        upstream_type_name,
        object(),
    )

    with pytest.raises(
        ExecutionQuoteChainUnavailable,
        match="canonical verified execution view authority is unavailable",
    ):
        _project(ledger)
