from __future__ import annotations

from dataclasses import replace
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
        campaign_id="campaign-1",
        scientific_protocol_sha256=HASH_A,
        candidate_universe_rule_id="all-observed-v1",
        candidate_universe_rule_sha256=HASH_B,
        forward_evaluation_policy_sha256=HASH_C,
        runtime_identity_sha256=HASH_D,
        baseline_set_sha256=HASH_E,
        protective_metric_set_sha256=HASH_F,
        cost_policy_sha256="1" * 64,
        precommit_anchor_lower=BASE,
        precommit_anchor_upper=BASE + timedelta(seconds=1),
    )


def _one_candidate_campaign() -> CampaignEvidence:
    proto = _protocol()
    observed = BASE + timedelta(seconds=10)
    opportunity = ForwardOpportunityEnvelope(
        campaign_id=proto.campaign_id,
        protocol_sha256=proto.protocol_sha256,
        candidate_sequence=1,
        opportunity_id="opportunity-1",
        source_receipt_id="receipt-1",
        source_receipt_sha256=HASH_B,
        causal_cutoff=observed,
        observed_lower=observed,
        observed_upper=observed + timedelta(milliseconds=10),
        universe_rule_result=UniverseResult.ADMITTED,
        universe_rule_reason_code="MATCH",
        provider_acquisition_state="OK",
        reveal_boundary_receipt_id="boundary-1",
        runtime_identity_sha256=proto.runtime_identity_sha256,
        predecessor_opportunity_sha256="GENESIS",
        decision_state=DecisionState.ACTION,
        decision_id="decision-1",
        quote_or_market_identity="market-1",
    )
    root = build_cohort_root(
        (opportunity,),
        anchor_lower=BASE + timedelta(seconds=20),
        anchor_upper=BASE + timedelta(seconds=21),
    )
    close = CampaignCloseEnvelope(
        campaign_id=proto.campaign_id,
        protocol_sha256=proto.protocol_sha256,
        terminal_cohort_root_sha256=root.cohort_root_sha256,
        final_candidate_count=1,
        first_sequence=1,
        last_sequence=1,
        close_reason="WINDOW_COMPLETE",
        anchor_lower=BASE + timedelta(seconds=110),
        anchor_upper=BASE + timedelta(seconds=111),
    )
    boundary = RevealBoundaryReceipt(
        receipt_id="boundary-1",
        campaign_id=proto.campaign_id,
        event_or_market_id="market-1",
        provider_or_authority_id="provider-1",
        boundary_rule_id="PREMATCH_TO_INPLAY",
        source_receipt_id="boundary-source-1",
        source_sha256=HASH_A,
        boundary_lower=BASE + timedelta(seconds=100),
        boundary_upper=BASE + timedelta(seconds=101),
        uncertainty_basis="provider-state",
    )
    authoritative = AuthoritativeSourceReceipt(
        receipt_id=opportunity.source_receipt_id,
        receipt_sha256=opportunity.source_receipt_sha256,
        campaign_id=proto.campaign_id,
        opportunity_id=opportunity.opportunity_id,
        universe_rule_result=opportunity.universe_rule_result,
    )
    evidence = CampaignEvidence(
        protocol=proto,
        opportunities=(opportunity,),
        cohort_roots=(root,),
        closes=(close,),
        reveal_boundaries=(boundary,),
        authoritative_receipts=(authoritative,),
        denominator_sequences=(1,),
        cost_evidence=(CostEvidence(1, True),),
        safety_margin=timedelta(seconds=1),
    )
    assert verify_campaign(evidence).codes == (VerificationCode.PASS,)
    return evidence


def test_closed_nonempty_claim_cannot_pass_with_zero_opportunities() -> None:
    proto = _protocol()
    forged_close = CampaignCloseEnvelope(
        campaign_id=proto.campaign_id,
        protocol_sha256=proto.protocol_sha256,
        terminal_cohort_root_sha256=HASH_A,
        final_candidate_count=1,
        first_sequence=1,
        last_sequence=1,
        close_reason="FORGED_EMPTY_CLOSE",
        anchor_lower=BASE + timedelta(seconds=110),
        anchor_upper=BASE + timedelta(seconds=111),
    )
    result = verify_campaign(
        CampaignEvidence(
            protocol=proto,
            opportunities=(),
            cohort_roots=(),
            closes=(forged_close,),
            reveal_boundaries=(),
            authoritative_receipts=(),
            denominator_sequences=(),
            cost_evidence=(),
        )
    )

    assert result.ok is False
    assert VerificationCode.PASS not in result.codes


def test_every_opportunity_requires_exact_authoritative_receipt() -> None:
    data = _one_candidate_campaign()

    result = verify_campaign(replace(data, authoritative_receipts=()))

    assert result.ok is False
    assert VerificationCode.PASS not in result.codes


def test_cross_campaign_reveal_boundary_cannot_authorize_candidate() -> None:
    data = _one_candidate_campaign()
    foreign_boundary = replace(
        data.reveal_boundaries[0],
        campaign_id="different-campaign",
    )

    result = verify_campaign(
        replace(data, reveal_boundaries=(foreign_boundary,))
    )

    assert result.ok is False
    assert VerificationCode.PASS not in result.codes


def test_late_favorable_boundary_correction_cannot_retroactively_mint_pass() -> None:
    data = _one_candidate_campaign()
    original = replace(
        data.reveal_boundaries[0],
        boundary_lower=BASE + timedelta(seconds=22),
        boundary_upper=BASE + timedelta(seconds=23),
    )
    historical = verify_campaign(replace(data, reveal_boundaries=(original,)))
    assert VerificationCode.TEMPORAL_ELIGIBILITY_UNKNOWN in historical.codes

    later_correction = RevealBoundaryReceipt(
        receipt_id="boundary-2",
        campaign_id=original.campaign_id,
        event_or_market_id=original.event_or_market_id,
        provider_or_authority_id=original.provider_or_authority_id,
        boundary_rule_id=original.boundary_rule_id,
        source_receipt_id="boundary-source-late",
        source_sha256=HASH_C,
        boundary_lower=BASE + timedelta(seconds=100),
        boundary_upper=BASE + timedelta(seconds=101),
        uncertainty_basis="late-provider-correction",
        supersedes_receipt_id=original.receipt_id,
    )

    restated = verify_campaign(
        replace(
            data,
            reveal_boundaries=(original, later_correction),
        )
    )

    assert restated.ok is False
    assert VerificationCode.TEMPORAL_ELIGIBILITY_UNKNOWN in restated.codes
