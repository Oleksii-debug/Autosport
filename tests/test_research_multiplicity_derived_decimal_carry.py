from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.research_multiplicity import (
    ExperimentFamilyMember,
    ExperimentFamilyPlan,
    MetricDirection,
    MultiplicityControlKind,
)


def _member() -> ExperimentFamilyMember:
    return ExperimentFamilyMember(
        hypothesis_id="derived-carry-probe",
        hypothesis_sha256="a" * 64,
        semantic_variant_sha256="b" * 64,
        candidate_label="derived carry probe",
    )


def test_derived_4097_digit_carry_reaches_family_budget_check() -> None:
    # Each caller-supplied spend is independently within the 4096-digit input
    # resource fence and numerically equals 0.5.  Exact addition is allowed to
    # grow by one carry digit: 0.5 + 0.5 becomes a derived Decimal with a
    # 4097-digit coefficient at exponent -4096.  That internal result is not
    # fresh evidence and must reach the existing semantic family-budget check.
    spend = Decimal((0, (5,) + ((0,) * 4095), -4096))
    assert len(spend.as_tuple().digits) == 4096
    assert spend == Decimal("0.5")

    with pytest.raises(
        ValueError,
        match="predeclared family alpha spending exceeds familywise_alpha",
    ):
        ExperimentFamilyPlan(
            family_id="derived-carry-family",
            research_protocol_id="protocol-derived-carry",
            protocol_sha256="c" * 64,
            research_question_id="derived-carry-question",
            primary_metric="net_utility",
            direction=MetricDirection.HIGHER_IS_BETTER,
            control_kind=MultiplicityControlKind.FWER,
            method_id=ExperimentFamilyPlan.IMPLEMENTED_METHOD,
            stopping_rule="two exact spends",
            familywise_alpha=Decimal("0.9"),
            look_alpha_spend=(spend, spend),
            members=(_member(),),
            frozen_at="2026-09-24T21:00:00Z",
        )
