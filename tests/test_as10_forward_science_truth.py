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
from test_forward_evidence_completeness import BASE, campaign


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


@pytest.mark.parametrize("successor_second", [23, 25, 26])
def test_successor_root_anchor_must_follow_predecessor_interval(
    successor_second: int,
) -> None:
    """Two individually valid roots with correctly rebound hashes still fail.

    Both intervals precede the unchanged reveal boundary (+100 seconds).
    The first root is anchored at [+25,+26], then the successor claims
    [+23,+24] (reversed), [+25,+26] (overlap), or
    [+26,+27] (same boundary instant, no provable causal order).
    """
    baseline = campaign(states=(DecisionState.NO_BET, DecisionState.NO_BET))
    assert verify_campaign(baseline).ok
    first, second = baseline.cohort_roots
    first = replace(
        first,
        anchor_lower=BASE + timedelta(seconds=25),
        anchor_upper=BASE + timedelta(seconds=26),
    )
    second = replace(
        second,
        previous_cohort_root_sha256=first.cohort_root_sha256,
        anchor_lower=BASE + timedelta(seconds=successor_second),
        anchor_upper=BASE + timedelta(seconds=successor_second + 1),
    )
    close = replace(
        baseline.closes[0],
        terminal_cohort_root_sha256=second.cohort_root_sha256,
    )
    altered = replace(baseline, cohort_roots=(first, second), closes=(close,))
    verdict = verify_campaign(altered)
    assert not verdict.ok
    assert VerificationCode.COHORT_ROOT_MISMATCH in verdict.codes
    assert ("root_anchor_chain_order", second.cohort_root_sha256) in verdict.details


def test_close_anchor_equal_terminal_root_end_is_not_causal_order() -> None:
    """Close and terminal root cannot be proven ordered at the same instant."""
    baseline = campaign(states=(DecisionState.NO_BET,))
    assert verify_campaign(baseline).ok
    terminal_root = baseline.cohort_roots[-1]
    assert terminal_root.anchor_upper is not None
    close = replace(
        baseline.closes[0],
        anchor_lower=terminal_root.anchor_upper,
        anchor_upper=terminal_root.anchor_upper + timedelta(seconds=1),
    )
    verdict = verify_campaign(replace(baseline, closes=(close,)))
    assert not verdict.ok
    assert VerificationCode.COHORT_ROOT_MISMATCH in verdict.codes
    assert ("close_anchor_causal_order", close.close_sha256) in verdict.details

@pytest.mark.parametrize(
    ("candidate_count", "root_every"),
    [(6, 5), (7, 2), (9, 2), (34, 3), (39, 7), (50, 3)],
)
def test_partial_final_cohort_fixture_preserves_strict_anchor_chronology(
    candidate_count: int, root_every: int,
) -> None:
    """An expanding final cohort must preserve evidence before reveal."""
    baseline = campaign(
        states=(DecisionState.NO_BET,) * candidate_count,
        root_every=root_every,
    )
    verdict = verify_campaign(baseline)
    assert verdict.ok, verdict
    for previous, successor in zip(
        baseline.cohort_roots, baseline.cohort_roots[1:]
    ):
        assert previous.anchor_upper is not None
        assert successor.anchor_lower is not None
        assert previous.anchor_upper < successor.anchor_lower
