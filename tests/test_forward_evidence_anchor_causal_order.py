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


def _campaign(*, root_lower, root_upper, close_lower, close_upper) -> CampaignEvidence:
    protocol = ForwardEvidenceProtocolEnvelope(
        campaign_id="anchor-order-campaign",
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
    observed = BASE + timedelta(seconds=11)
    opportunity = ForwardOpportunityEnvelope(
        campaign_id=protocol.campaign_id,
        protocol_sha256=protocol.protocol_sha256,
        candidate_sequence=1,
        opportunity_id="opportunity-1",
        source_receipt_id="receipt-1",
        source_receipt_sha256="2" * 64,
        causal_cutoff=observed,
        observed_lower=observed,
        observed_upper=observed + timedelta(milliseconds=10),
        universe_rule_result=UniverseResult.ADMITTED,
        universe_rule_reason_code="MATCH",
        provider_acquisition_state="OK",
        reveal_boundary_receipt_id="boundary-1",
        runtime_identity_sha256=protocol.runtime_identity_sha256,
        predecessor_opportunity_sha256="GENESIS",
        decision_state=DecisionState.ACTION,
        decision_id="decision-1",
        quote_or_market_identity="market-1",
    )
    root = build_cohort_root(
        (opportunity,),
        anchor_lower=root_lower,
        anchor_upper=root_upper,
    )
    close = CampaignCloseEnvelope(
        campaign_id=protocol.campaign_id,
        protocol_sha256=protocol.protocol_sha256,
        terminal_cohort_root_sha256=root.cohort_root_sha256,
        final_candidate_count=1,
        first_sequence=1,
        last_sequence=1,
        close_reason="WINDOW_COMPLETE",
        anchor_lower=close_lower,
        anchor_upper=close_upper,
    )
    boundary = RevealBoundaryReceipt(
        receipt_id="boundary-1",
        campaign_id=protocol.campaign_id,
        event_or_market_id="event-1",
        provider_or_authority_id="provider-1",
        boundary_rule_id="PREMATCH_TO_INPLAY",
        source_receipt_id="boundary-source-1",
        source_sha256=HASH_A,
        boundary_lower=BASE + timedelta(seconds=100),
        boundary_upper=BASE + timedelta(seconds=101),
        uncertainty_basis="provider-state",
    )
    receipt = AuthoritativeSourceReceipt(
        receipt_id=opportunity.source_receipt_id,
        receipt_sha256=opportunity.source_receipt_sha256,
        campaign_id=protocol.campaign_id,
        opportunity_id=opportunity.opportunity_id,
        universe_rule_result=UniverseResult.ADMITTED,
    )
    return CampaignEvidence(
        protocol=protocol,
        opportunities=(opportunity,),
        cohort_roots=(root,),
        closes=(close,),
        reveal_boundaries=(boundary,),
        authoritative_receipts=(receipt,),
        denominator_sequences=(1,),
        cost_evidence=(CostEvidence(1, True),),
        safety_margin=timedelta(seconds=1),
    )


def test_cohort_root_cannot_be_anchored_before_included_observation() -> None:
    data = _campaign(
        root_lower=BASE + timedelta(seconds=5),
        root_upper=BASE + timedelta(seconds=6),
        close_lower=BASE + timedelta(seconds=110),
        close_upper=BASE + timedelta(seconds=111),
    )

    result = verify_campaign(data)

    assert result.ok is False, (
        "a cumulative cohort root cannot causally commit an observation that "
        "occurs only after the root anchor interval"
    )
    assert "COHORT_ROOT_MISMATCH" in {code.value for code in result.codes}
    assert ("root_anchor_causal_order", data.cohort_roots[0].cohort_root_sha256) in (
        result.details
    )


def test_closed_campaign_anchor_cannot_predate_terminal_root_anchor() -> None:
    data = _campaign(
        root_lower=BASE + timedelta(seconds=20),
        root_upper=BASE + timedelta(seconds=21),
        close_lower=BASE + timedelta(seconds=5),
        close_upper=BASE + timedelta(seconds=6),
    )

    result = verify_campaign(data)

    assert result.ok is False, (
        "a CLOSED campaign cannot causally commit a terminal root whose anchor "
        "occurs only after the close anchor interval"
    )
    assert "COHORT_ROOT_MISMATCH" in {code.value for code in result.codes}
    assert ("close_anchor_causal_order", data.closes[0].close_sha256) in (
        result.details
    )
