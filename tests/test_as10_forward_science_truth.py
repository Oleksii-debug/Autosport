"""Fail-closed structural science regressions on exact forward cohort denominator.

These fixture-level falsifiers do not prove that a provider's universe is
exhaustive, a multi-day autonomous campaign occurred, or promotion is safe.
"""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import inspect

import pytest

from autosport.forward_evidence_completeness import (
    DecisionState,
    ForwardEvidenceCompletenessError,
    VerificationCode,
    VerificationResult,
    verify_campaign,
)
from test_forward_evidence_completeness import campaign


class IntImpostor(int):
    pass


@pytest.mark.parametrize(
    "forged",
    [
        (True,),
        (1.0,),
        (Decimal("1"),),
        (1 + 0j,),
        (IntImpostor(1),),
        [1],
        (0,),
        (-1,),
        (1, "bad"),
        (None,),
    ],
)
def test_bad_denominator_cannot_earn_structural_pass(forged: object) -> None:
    baseline = campaign(states=(DecisionState.NO_BET,))
    assert verify_campaign(baseline).ok
    verdict = verify_campaign(replace(baseline, denominator_sequences=forged))
    assert verdict.ok is False
    assert VerificationCode.DENOMINATOR_INCOMPLETE in verdict.codes


def test_exact_positive_int_sequence_still_passes_structurally() -> None:
    baseline = campaign(states=(DecisionState.NO_BET,))
    assert type(baseline.denominator_sequences) is tuple
    assert type(baseline.denominator_sequences[0]) is int
    verdict = verify_campaign(baseline)
    assert verdict.ok
    assert verdict.verification_scope == "STRUCTURAL_ONLY"
    assert verdict.external_universe_authority_resolved is False
    assert verdict.promotion_ready is False


def test_structural_pass_truth_fields_are_outputs_not_constructor_claims() -> None:
    args = inspect.signature(VerificationResult).parameters
    assert all(name not in args for name in (
        "verification_scope",
        "external_universe_authority_resolved",
        "promotion_ready",
    ))


@pytest.mark.parametrize(
    ("component", "field"),
    [
        ("opportunity", "candidate_sequence"),
        ("root", "first_sequence"),
        ("root", "last_sequence"),
        ("root", "candidate_count"),
        ("close", "first_sequence"),
        ("close", "last_sequence"),
        ("close", "final_candidate_count"),
        ("cost", "candidate_sequence"),
    ],
)
def test_sequence_identity_rejects_int_subclass(component: str, field: str) -> None:
    baseline = campaign(states=(DecisionState.NO_BET,))
    assert verify_campaign(baseline).ok
    components = {
        "opportunity": baseline.opportunities[0],
        "root": baseline.cohort_roots[0],
        "close": baseline.closes[0],
        "cost": baseline.cost_evidence[0],
    }
    with pytest.raises(ForwardEvidenceCompletenessError, match="positive integer"):
        replace(components[component], **{field: IntImpostor(1)})


def test_unrecorded_candidate_cost_cannot_disappear_from_denominator() -> None:
    baseline = campaign(states=(DecisionState.NO_BET,))
    assert verify_campaign(baseline).ok
    extra_cost = replace(
        baseline.cost_evidence[0],
        candidate_sequence=baseline.opportunities[-1].candidate_sequence + 1,
    )
    result = verify_campaign(replace(
        baseline, cost_evidence=baseline.cost_evidence + (extra_cost,),
    ))
    assert not result.ok
    assert VerificationCode.DENOMINATOR_INCOMPLETE in result.codes


@pytest.mark.parametrize("far_sequence", (3, 1 << 70))
def test_sparse_candidate_sequence_returns_verdict_without_range_overflow(
    far_sequence: int,
) -> None:
    baseline = campaign(states=(DecisionState.NO_BET,))
    assert verify_campaign(baseline).ok
    anchor = baseline.opportunities[0]
    unexpected = replace(
        anchor,
        candidate_sequence=far_sequence,
        opportunity_id=f"unexpected-opportunity-{far_sequence}",
        source_receipt_id=f"unexpected-receipt-{far_sequence}",
        predecessor_opportunity_sha256=anchor.opportunity_sha256,
    )
    result = verify_campaign(replace(
        baseline, opportunities=baseline.opportunities + (unexpected,),
    ))
    assert not result.ok
    assert VerificationCode.SEQUENCE_GAP in result.codes


@pytest.mark.parametrize("margin", [timedelta(days=1_000_000), timedelta.max])
def test_enormous_safety_margin_fails_closed_without_datetime_underflow(
    margin: timedelta,
) -> None:
    baseline = campaign(states=(DecisionState.NO_BET,))
    assert verify_campaign(baseline).ok
    verdict = verify_campaign(replace(baseline, safety_margin=margin))
    assert not verdict.ok
    assert VerificationCode.TEMPORAL_ELIGIBILITY_UNKNOWN in verdict.codes
