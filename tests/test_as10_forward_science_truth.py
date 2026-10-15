"""Fail-closed structural science regressions on exact forward cohort denominator.

These fixture-level falsifiers do not prove that a provider's universe is
exhaustive, a multi-day autonomous campaign occurred, or promotion is safe.
"""
from dataclasses import replace
from decimal import Decimal
import inspect

import pytest

from autosport.forward_evidence_completeness import (
    DecisionState,
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
