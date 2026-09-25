from decimal import Decimal

import pytest

from autosport.portfolio_joint_stress import (
    JointDependenceGrade,
    JointDependenceRelation,
    _decimal_text,
)


def test_decimal_text_rejects_huge_negative_exponent_before_fixed_point_expansion() -> None:
    with pytest.raises(ValueError, match="resource bounds"):
        _decimal_text(Decimal("1E-1000000"))


def test_decimal_text_rejects_huge_coefficient_shape() -> None:
    coefficient = Decimal("0." + ("1" * 4097))
    with pytest.raises(ValueError, match="resource bounds"):
        _decimal_text(coefficient)


def test_decimal_text_applies_resource_bound_before_zero_shortcut() -> None:
    with pytest.raises(ValueError, match="resource bounds"):
        _decimal_text(Decimal("0E-1000000"))


def test_empirical_relation_hash_fails_closed_on_attacker_shaped_decimal() -> None:
    relation = JointDependenceRelation(
        relation_id="empirical-resource-bound",
        member_ticket_ids=("ticket-a", "ticket-b"),
        grade=JointDependenceGrade.EMPIRICAL_DEPENDENCE,
        reason="resource-bound regression",
        evidence_sha256="a" * 64,
        evidence_available_at="2026-09-25T00:00:00+00:00",
        empirical_coefficient=Decimal("1E-1000000"),
    )

    with pytest.raises(ValueError, match="resource bounds"):
        _ = relation.relation_sha256


def test_decimal_text_keeps_normal_exact_decimal_semantics() -> None:
    assert _decimal_text(Decimal("0.125000")) == "0.125"
    assert _decimal_text(Decimal("-1")) == "-1"
    assert _decimal_text(Decimal("1E+3")) == "1000"
