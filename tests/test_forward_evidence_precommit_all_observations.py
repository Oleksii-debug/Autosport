from __future__ import annotations

from datetime import UTC, datetime, timedelta

from autosport.forward_evidence_completeness import (
    AuthoritativeSourceReceipt,
    CampaignCloseEnvelope,
    CampaignEvidence,
    CostEvidence,
    DecisionState,
    ForwardEvidenceProtocolEnvelope,
    ForwardOpportunityEnvelope,
    RevealBoundaryReceipt,
    UniverseResult,
    VerificationCode,
    build_cohort_root,
    verify_campaign,
)


BASE = datetime(2026, 1, 1, tzinfo=UTC)
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
HASH_E = "e" * 64
HASH_F = "f" * 64


def _protocol() -> ForwardEvidenceProtocolEnvelope:
    return ForwardEvidenceProtocolEnvelope(
        campaign_id="campaign-precommit-all-observations",
        scientific_protocol_sha256=HASH_A,
        candidate_universe_rule_id="all-observed-v1",
        candidate_universe_rule_sha256=HASH_B,
        forward_evaluation_policy_sha256=HASH_C,
        runtime_identity_sha256=HASH_D,
        baseline_set_sha256=HASH_E,
        protective_metric_set_sha256=HASH_F,
        cost_policy_sha256="1" * 64,
        precommit_anchor_lower=BASE + timedelta(seconds=10),
        precommit_anchor_upper=BASE + timedelta(seconds=11),
    )


def _opportunity(
    proto: ForwardEvidenceProtocolEnvelope,
    *,
    sequence: int,
    predecessor: str,
    observed_lower: datetime,
) -> ForwardOpportunityEnvelope:
    return ForwardOpportunityEnvelope(
        campaign_id=proto.campaign_id,
        protocol_sha256=proto.protocol_sha256,
        candidate_sequence=sequence,
        opportunity_id=f"opportunity-{sequence}",
        source_receipt_id=f"receipt-{sequence}",
        source_receipt_sha256=str(sequence) * 64,
        causal_cutoff=observed_lower,
        observed_lower=observed_lower,
        observed_upper=observed_lower + timedelta(milliseconds=10),
        universe_rule_result=UniverseResult.ADMITTED,
        universe_rule_reason_code="MATCH",
        provider_acquisition_state="OK",
        reveal_boundary_receipt_id="boundary-1",
        runtime_identity_sha256=proto.runtime_identity_sha256,
        predecessor_opportunity_sha256=predecessor,
        decision_state=DecisionState.ACTION,
        decision_id=f"decision-{sequence}",
        quote_or_market_identity=f"market-{sequence}",
    )


def _campaign(*, second_observed_lower: datetime) -> CampaignEvidence:
    proto = _protocol()
    first = _opportunity(
        proto,
        sequence=1,
        predecessor="GENESIS",
        observed_lower=BASE + timedelta(seconds=20),
    )
    second = _opportunity(
        proto,
        sequence=2,
        predecessor=first.opportunity_sha256,
        observed_lower=second_observed_lower,
    )
    opportunities = (first, second)

    root = build_cohort_root(
        opportunities,
        anchor_lower=BASE + timedelta(seconds=30),
        anchor_upper=BASE + timedelta(seconds=31),
    )
    close = CampaignCloseEnvelope(
        campaign_id=proto.campaign_id,
        protocol_sha256=proto.protocol_sha256,
        terminal_cohort_root_sha256=root.cohort_root_sha256,
        final_candidate_count=2,
        first_sequence=1,
        last_sequence=2,
        close_reason="WINDOW_COMPLETE",
        anchor_lower=BASE + timedelta(seconds=110),
        anchor_upper=BASE + timedelta(seconds=111),
    )
    boundary = RevealBoundaryReceipt(
        receipt_id="boundary-1",
        campaign_id=proto.campaign_id,
        event_or_market_id="event-1",
        provider_or_authority_id="provider-1",
        boundary_rule_id="PREMATCH_TO_INPLAY",
        source_receipt_id="boundary-source-1",
        source_sha256=HASH_A,
        boundary_lower=BASE + timedelta(seconds=100),
        boundary_upper=BASE + timedelta(seconds=101),
        uncertainty_basis="provider-state",
    )
    receipts = tuple(
        AuthoritativeSourceReceipt(
            receipt_id=item.source_receipt_id,
            receipt_sha256=item.source_receipt_sha256,
            campaign_id=proto.campaign_id,
            opportunity_id=item.opportunity_id,
            universe_rule_result=item.universe_rule_result,
        )
        for item in opportunities
    )
    return CampaignEvidence(
        protocol=proto,
        opportunities=opportunities,
        cohort_roots=(root,),
        closes=(close,),
        reveal_boundaries=(boundary,),
        authoritative_receipts=receipts,
        denominator_sequences=(1, 2),
        cost_evidence=(CostEvidence(1, True), CostEvidence(2, True)),
        safety_margin=timedelta(seconds=1),
    )


def test_precommit_must_precede_every_included_observation() -> None:
    """Sequence ordering must not stand in for observation-time ordering."""

    positive_control = _campaign(
        second_observed_lower=BASE + timedelta(seconds=21),
    )
    assert verify_campaign(positive_control).codes == (VerificationCode.PASS,)

    adversarial = _campaign(
        second_observed_lower=BASE + timedelta(seconds=5),
    )
    result = verify_campaign(adversarial)

    assert result.ok is False
    assert VerificationCode.PROTOCOL_PRECOMMIT_FAIL in result.codes
    assert VerificationCode.PASS not in result.codes
