from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from autosport.forward_evidence_completeness import (
    AuthoritativeSourceReceipt,
    CampaignCloseEnvelope,
    CampaignCloseState,
    CampaignEvidence,
    CostEvidence,
    DecisionState,
    ForwardOpportunityEnvelope,
    ForwardEvidenceProtocolEnvelope,
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


def protocol(**changes: object) -> ForwardEvidenceProtocolEnvelope:
    return replace(
        ForwardEvidenceProtocolEnvelope(
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
        ),
        **changes,
    )


def opportunity(
    proto: ForwardEvidenceProtocolEnvelope,
    sequence: int,
    *,
    predecessor: str,
    state: DecisionState = DecisionState.ACTION,
    receipt_id: str | None = None,
    opportunity_id: str | None = None,
    boundary_id: str = "boundary-1",
    runtime_identity_sha256: str | None = None,
) -> ForwardOpportunityEnvelope:
    observed = BASE + timedelta(seconds=10 + sequence)
    return ForwardOpportunityEnvelope(
        campaign_id=proto.campaign_id,
        protocol_sha256=proto.protocol_sha256,
        candidate_sequence=sequence,
        opportunity_id=opportunity_id or f"opportunity-{sequence}",
        source_receipt_id=receipt_id or f"receipt-{sequence}",
        source_receipt_sha256=(f"{sequence:x}" * 64)[:64],
        causal_cutoff=observed,
        observed_lower=observed,
        observed_upper=observed + timedelta(milliseconds=10),
        universe_rule_result=UniverseResult.ADMITTED,
        universe_rule_reason_code="MATCH",
        provider_acquisition_state="OK",
        reveal_boundary_receipt_id=boundary_id,
        runtime_identity_sha256=(
            runtime_identity_sha256 or proto.runtime_identity_sha256
        ),
        predecessor_opportunity_sha256=predecessor,
        decision_state=state,
        decision_id=(f"decision-{sequence}" if state is DecisionState.ACTION else None),
        quote_or_market_identity=f"market-{sequence}",
    )


def campaign(
    *,
    states: tuple[DecisionState, ...] = (
        DecisionState.ACTION,
        DecisionState.NO_BET,
        DecisionState.RISK_REJECT,
    ),
    root_every: int = 1,
) -> CampaignEvidence:
    proto = protocol()
    opportunities: list[ForwardOpportunityEnvelope] = []
    predecessor = "GENESIS"
    for sequence, state in enumerate(states, start=1):
        item = opportunity(
            proto,
            sequence,
            predecessor=predecessor,
            state=state,
        )
        opportunities.append(item)
        predecessor = item.opportunity_sha256

    roots = []
    previous_root = "GENESIS"
    for last in range(root_every, len(opportunities) + 1, root_every):
        root = build_cohort_root(
            opportunities[:last],
            previous_cohort_root_sha256=previous_root,
            anchor_lower=BASE + timedelta(seconds=20 + last),
            anchor_upper=BASE + timedelta(seconds=21 + last),
        )
        roots.append(root)
        previous_root = root.cohort_root_sha256
    if not roots or roots[-1].last_sequence != len(opportunities):
        root = build_cohort_root(
            opportunities,
            previous_cohort_root_sha256=previous_root,
            anchor_lower=BASE + timedelta(seconds=30),
            anchor_upper=BASE + timedelta(seconds=31),
        )
        roots.append(root)

    close = CampaignCloseEnvelope(
        campaign_id=proto.campaign_id,
        protocol_sha256=proto.protocol_sha256,
        terminal_cohort_root_sha256=roots[-1].cohort_root_sha256,
        final_candidate_count=len(opportunities),
        first_sequence=1,
        last_sequence=len(opportunities),
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
            universe_rule_result=UniverseResult.ADMITTED,
        )
        for item in opportunities
    )
    return CampaignEvidence(
        protocol=proto,
        opportunities=tuple(opportunities),
        cohort_roots=tuple(roots),
        closes=(close,),
        reveal_boundaries=(boundary,),
        authoritative_receipts=receipts,
        denominator_sequences=tuple(range(1, len(opportunities) + 1)),
        cost_evidence=tuple(
            CostEvidence(sequence, True)
            for sequence in range(1, len(opportunities) + 1)
        ),
        safety_margin=timedelta(seconds=1),
    )


def has(result, code: VerificationCode) -> bool:
    return code in result.codes


def test_valid_complete_campaign_passes() -> None:
    result = verify_campaign(campaign())
    assert result.ok is True
    assert result.codes == (VerificationCode.PASS,)


def test_missing_middle_candidate_sequence_fails() -> None:
    data = campaign()
    opportunities = (data.opportunities[0], data.opportunities[2])
    result = verify_campaign(replace(data, opportunities=opportunities))
    assert has(result, VerificationCode.SEQUENCE_GAP)


def test_terminal_unfavorable_candidate_cannot_be_truncated() -> None:
    data = campaign()
    truncated = data.opportunities[:2]
    root = build_cohort_root(
        truncated,
        anchor_lower=BASE + timedelta(seconds=30),
        anchor_upper=BASE + timedelta(seconds=31),
    )
    close = CampaignCloseEnvelope(
        campaign_id=data.protocol.campaign_id,
        protocol_sha256=data.protocol.protocol_sha256,
        terminal_cohort_root_sha256=root.cohort_root_sha256,
        final_candidate_count=2,
        first_sequence=1,
        last_sequence=2,
        close_reason="EARLY_CLOSE",
        anchor_lower=BASE + timedelta(seconds=110),
        anchor_upper=BASE + timedelta(seconds=111),
    )
    result = verify_campaign(
        replace(
            data,
            opportunities=truncated,
            cohort_roots=(root,),
            closes=(close,),
            denominator_sequences=(1, 2),
            cost_evidence=(CostEvidence(1, True), CostEvidence(2, True)),
        )
    )
    assert has(result, VerificationCode.COHORT_OMISSION_DETECTED)


def test_identical_duplicate_candidate_is_idempotent() -> None:
    data = campaign()
    result = verify_campaign(
        replace(data, opportunities=data.opportunities + (data.opportunities[1],))
    )
    assert result.ok is True
    assert result.candidate_count == 3


def test_conflicting_duplicate_sequence_fails_identity() -> None:
    data = campaign()
    conflicting = opportunity(
        data.protocol,
        2,
        predecessor=data.opportunities[0].opportunity_sha256,
        opportunity_id="different-opportunity",
    )
    result = verify_campaign(
        replace(data, opportunities=data.opportunities + (conflicting,))
    )
    assert has(result, VerificationCode.EVIDENCE_IDENTITY_CONFLICT)


def test_tampered_ordered_root_fails() -> None:
    data = campaign()
    root = replace(data.cohort_roots[-1], ordered_leaf_sha256=HASH_A)
    close = replace(
        data.closes[0],
        terminal_cohort_root_sha256=root.cohort_root_sha256,
    )
    result = verify_campaign(replace(data, cohort_roots=(root,), closes=(close,)))
    assert has(result, VerificationCode.COHORT_ROOT_MISMATCH)


def test_valid_root_over_favorable_subset_fails_terminal_completeness() -> None:
    data = campaign()
    root = build_cohort_root(
        data.opportunities[:1],
        anchor_lower=BASE + timedelta(seconds=20),
        anchor_upper=BASE + timedelta(seconds=21),
    )
    close = CampaignCloseEnvelope(
        campaign_id=data.protocol.campaign_id,
        protocol_sha256=data.protocol.protocol_sha256,
        terminal_cohort_root_sha256=root.cohort_root_sha256,
        final_candidate_count=1,
        first_sequence=1,
        last_sequence=1,
        close_reason="FAVORABLE_PREFIX",
        anchor_lower=BASE + timedelta(seconds=110),
        anchor_upper=BASE + timedelta(seconds=111),
    )
    result = verify_campaign(replace(data, cohort_roots=(root,), closes=(close,)))
    assert has(result, VerificationCode.COHORT_ROOT_MISMATCH)


def test_authoritative_admitted_source_receipt_cannot_disappear() -> None:
    data = campaign()
    extra = AuthoritativeSourceReceipt(
        receipt_id="receipt-extra",
        receipt_sha256=HASH_A,
        campaign_id=data.protocol.campaign_id,
        opportunity_id="opportunity-extra",
        universe_rule_result=UniverseResult.ADMITTED,
    )
    result = verify_campaign(
        replace(data, authoritative_receipts=data.authoritative_receipts + (extra,))
    )
    assert has(result, VerificationCode.COHORT_OMISSION_DETECTED)


def test_authoritative_excluded_candidate_cannot_disappear() -> None:
    data = campaign()
    excluded = AuthoritativeSourceReceipt(
        receipt_id="receipt-excluded",
        receipt_sha256=HASH_B,
        campaign_id=data.protocol.campaign_id,
        opportunity_id="opportunity-excluded",
        universe_rule_result=UniverseResult.EXCLUDED,
    )
    result = verify_campaign(
        replace(data, authoritative_receipts=data.authoritative_receipts + (excluded,))
    )
    assert has(result, VerificationCode.COHORT_OMISSION_DETECTED)


def test_excluded_candidate_cannot_claim_action() -> None:
    data = campaign()
    with pytest.raises(ValueError, match="excluded candidate"):
        replace(
            data.opportunities[0],
            universe_rule_result=UniverseResult.EXCLUDED,
        )


def test_provider_failure_is_a_candidate_not_permission_to_erase_it() -> None:
    data = campaign(states=(DecisionState.ACTION, DecisionState.PROVIDER_FAILURE))
    assert verify_campaign(data).ok is True
    result = verify_campaign(
        replace(
            data,
            opportunities=(data.opportunities[0],),
            denominator_sequences=(1,),
            cost_evidence=(CostEvidence(1, True),),
        )
    )
    assert has(result, VerificationCode.COHORT_OMISSION_DETECTED)


def test_no_bet_and_risk_reject_must_remain_in_denominator() -> None:
    data = campaign()
    result = verify_campaign(replace(data, denominator_sequences=(1,)))
    assert has(result, VerificationCode.DENOMINATOR_INCOMPLETE)


def test_candidate_appended_after_terminal_close_cannot_silently_reopen() -> None:
    data = campaign()
    late = opportunity(
        data.protocol,
        4,
        predecessor=data.opportunities[-1].opportunity_sha256,
    )
    result = verify_campaign(
        replace(
            data,
            opportunities=data.opportunities + (late,),
            denominator_sequences=(1, 2, 3, 4),
            cost_evidence=data.cost_evidence + (CostEvidence(4, True),),
        )
    )
    assert has(result, VerificationCode.COHORT_ROOT_MISMATCH)


def test_duplicate_source_receipt_identity_fails_closed() -> None:
    data = campaign()
    duplicate = opportunity(
        data.protocol,
        4,
        predecessor=data.opportunities[-1].opportunity_sha256,
        receipt_id=data.opportunities[0].source_receipt_id,
    )
    result = verify_campaign(
        replace(
            data,
            opportunities=data.opportunities + (duplicate,),
            denominator_sequences=(1, 2, 3, 4),
            cost_evidence=data.cost_evidence + (CostEvidence(4, True),),
        )
    )
    assert has(result, VerificationCode.EVIDENCE_IDENTITY_CONFLICT)


def test_duplicate_decision_identity_fails_closed() -> None:
    data = campaign()
    duplicate = opportunity(
        data.protocol,
        4,
        predecessor=data.opportunities[-1].opportunity_sha256,
    )
    duplicate = replace(duplicate, decision_id=data.opportunities[0].decision_id)
    result = verify_campaign(
        replace(
            data,
            opportunities=data.opportunities + (duplicate,),
            denominator_sequences=(1, 2, 3, 4),
            cost_evidence=data.cost_evidence + (CostEvidence(4, True),),
        )
    )
    assert has(result, VerificationCode.EVIDENCE_IDENTITY_CONFLICT)


def test_missing_close_is_open_not_confirmatory_pass() -> None:
    result = verify_campaign(replace(campaign(), closes=()))
    assert has(result, VerificationCode.COHORT_OPEN)


def test_protocol_change_after_candidates_conflicts_with_committed_records() -> None:
    data = campaign()
    changed = protocol(forward_evaluation_policy_sha256="2" * 64)
    result = verify_campaign(replace(data, protocol=changed))
    assert has(result, VerificationCode.PROTOCOL_HASH_CONFLICT)


def test_same_model_label_with_different_runtime_bytes_conflicts() -> None:
    data = campaign()
    changed = replace(data.opportunities[1], runtime_identity_sha256=HASH_C)
    result = verify_campaign(
        replace(
            data,
            opportunities=(data.opportunities[0], changed, data.opportunities[2]),
        )
    )
    assert has(result, VerificationCode.RUNTIME_IDENTITY_CONFLICT)


def test_anchor_interval_overlapping_reveal_boundary_is_unknown() -> None:
    data = campaign(root_every=3)
    root = build_cohort_root(
        data.opportunities,
        anchor_lower=BASE + timedelta(seconds=99),
        anchor_upper=BASE + timedelta(seconds=100),
    )
    close = replace(
        data.closes[0],
        terminal_cohort_root_sha256=root.cohort_root_sha256,
    )
    result = verify_campaign(replace(data, cohort_roots=(root,), closes=(close,)))
    assert has(result, VerificationCode.TEMPORAL_ELIGIBILITY_UNKNOWN)


def test_late_response_does_not_invalidate_independent_preboundary_anchor_time() -> None:
    data = campaign(root_every=3)
    root = build_cohort_root(
        data.opportunities,
        anchor_lower=BASE + timedelta(seconds=90),
        anchor_upper=BASE + timedelta(seconds=91),
    )
    close = replace(
        data.closes[0],
        terminal_cohort_root_sha256=root.cohort_root_sha256,
    )
    result = verify_campaign(replace(data, cohort_roots=(root,), closes=(close,)))
    assert not has(result, VerificationCode.TEMPORAL_ELIGIBILITY_UNKNOWN)


def test_append_only_boundary_correction_can_invalidate_old_eligibility() -> None:
    data = campaign(root_every=3)
    correction = RevealBoundaryReceipt(
        receipt_id="boundary-2",
        campaign_id=data.protocol.campaign_id,
        event_or_market_id="event-1",
        provider_or_authority_id="provider-1",
        boundary_rule_id="PREMATCH_TO_INPLAY",
        source_receipt_id="boundary-source-2",
        source_sha256=HASH_B,
        boundary_lower=BASE + timedelta(seconds=25),
        boundary_upper=BASE + timedelta(seconds=26),
        uncertainty_basis="provider-correction",
        supersedes_receipt_id="boundary-1",
    )
    result = verify_campaign(
        replace(
            data,
            reveal_boundaries=data.reveal_boundaries + (correction,),
        )
    )
    assert has(result, VerificationCode.TEMPORAL_ELIGIBILITY_UNKNOWN)


def test_identical_recovery_copy_verifies_as_same_campaign() -> None:
    data = campaign()
    assert verify_campaign(data) == verify_campaign(data)


def test_two_distinct_successors_from_one_predecessor_are_a_fork() -> None:
    data = campaign()
    fork = opportunity(
        data.protocol,
        4,
        predecessor=data.opportunities[1].opportunity_sha256,
        opportunity_id="forked-opportunity",
    )
    result = verify_campaign(
        replace(
            data,
            opportunities=data.opportunities + (fork,),
            denominator_sequences=(1, 2, 3, 4),
            cost_evidence=data.cost_evidence + (CostEvidence(4, True),),
        )
    )
    assert has(result, VerificationCode.HASH_CHAIN_FORK)


def test_candidate_durable_before_any_root_is_not_confirmatory_pass() -> None:
    result = verify_campaign(replace(campaign(), cohort_roots=()))
    assert has(result, VerificationCode.COHORT_ROOT_MISMATCH)
    assert has(result, VerificationCode.TEMPORAL_ELIGIBILITY_UNKNOWN)


def test_unanchored_root_is_temporally_unknown() -> None:
    data = campaign(root_every=3)
    root = build_cohort_root(
        data.opportunities,
        anchor_lower=None,
        anchor_upper=None,
    )
    close = replace(
        data.closes[0],
        terminal_cohort_root_sha256=root.cohort_root_sha256,
    )
    result = verify_campaign(replace(data, cohort_roots=(root,), closes=(close,)))
    assert has(result, VerificationCode.TEMPORAL_ELIGIBILITY_UNKNOWN)


def test_settlement_correction_cannot_rewrite_pre_reveal_root() -> None:
    data = campaign()
    before = verify_campaign(data).terminal_root_sha256
    after = verify_campaign(data).terminal_root_sha256
    assert before == after


def test_unknown_material_cost_keeps_economics_incomplete() -> None:
    data = campaign()
    costs = (CostEvidence(1, True), CostEvidence(2, False), CostEvidence(3, True))
    result = verify_campaign(replace(data, cost_evidence=costs))
    assert has(result, VerificationCode.ECONOMICS_INCOMPLETE)


def test_duplicate_denominator_sequence_cannot_be_normalized_away() -> None:
    data = campaign()
    result = verify_campaign(replace(data, denominator_sequences=(1, 2, 2, 3)))
    assert has(result, VerificationCode.DENOMINATOR_INCOMPLETE)


def test_conflicting_duplicate_cost_evidence_fails_closed() -> None:
    data = campaign()
    costs = data.cost_evidence + (CostEvidence(2, False),)
    result = verify_campaign(replace(data, cost_evidence=costs))
    assert has(result, VerificationCode.EVIDENCE_IDENTITY_CONFLICT)
    assert has(result, VerificationCode.ECONOMICS_INCOMPLETE)


def test_close_pending_preserves_terminal_membership_but_is_not_complete() -> None:
    data = campaign()
    pending = replace(
        data.closes[0],
        close_state=CampaignCloseState.CLOSE_PENDING,
        anchor_lower=None,
        anchor_upper=None,
    )
    result = verify_campaign(replace(data, closes=(pending,)))
    assert has(result, VerificationCode.COHORT_CLOSE_PENDING)
    assert result.candidate_count == 3


def test_optional_stopping_violation_fails_confirmation() -> None:
    result = verify_campaign(replace(campaign(), stopping_rule_satisfied=False))
    assert has(result, VerificationCode.STOPPING_RULE_VIOLATION)


def test_protocol_must_be_anchored_before_first_candidate_admission() -> None:
    data = campaign()
    changed = protocol(precommit_anchor_upper=data.opportunities[0].observed_lower)
    result = verify_campaign(replace(data, protocol=changed))
    assert has(result, VerificationCode.PROTOCOL_PRECOMMIT_FAIL)


def test_missing_reveal_boundary_is_temporally_unknown() -> None:
    data = campaign()
    result = verify_campaign(replace(data, reveal_boundaries=()))
    assert has(result, VerificationCode.TEMPORAL_ELIGIBILITY_UNKNOWN)


def test_corrupt_periodic_root_chain_fails() -> None:
    data = campaign(root_every=1)
    assert len(data.cohort_roots) == 3
    corrupted = replace(data.cohort_roots[1], previous_cohort_root_sha256=HASH_A)
    result = verify_campaign(
        replace(
            data,
            cohort_roots=(data.cohort_roots[0], corrupted, data.cohort_roots[2]),
        )
    )
    assert has(result, VerificationCode.COHORT_ROOT_MISMATCH)


@pytest.mark.parametrize("sequence", range(2, 9))
def test_removing_any_interior_candidate_never_passes(sequence: int) -> None:
    states = tuple(DecisionState.ACTION for _ in range(9))
    data = campaign(states=states, root_every=3)
    opportunities = tuple(
        item for item in data.opportunities if item.candidate_sequence != sequence
    )
    result = verify_campaign(replace(data, opportunities=opportunities))
    assert result.ok is False
    assert has(result, VerificationCode.SEQUENCE_GAP)