from __future__ import annotations

from decimal import Decimal, ROUND_UP, localcontext

import pytest

from autosport.portfolio_plan import (
    PortfolioDependencyEvidence,
    RobustPortfolioProposal,
)


_A = "1" * 64
_B = "2" * 64
_I1 = "a" * 64
_I2 = "b" * 64
_PORTFOLIO = "c" * 64
_REPRO = "d" * 64


def _evidence(
    *,
    dependency: Decimal = Decimal("0.25"),
    uncertainty: Decimal = Decimal("0"),
    fee: Decimal = Decimal("0"),
    partial_fill: Decimal = Decimal("0"),
    pairs: object | None = None,
) -> PortfolioDependencyEvidence:
    pair_values = (
        ((_A, _B, dependency),)
        if pairs is None
        else pairs
    )
    return PortfolioDependencyEvidence(
        evidence_id="dependency-evidence-1",
        portfolio_sha256=_PORTFOLIO,
        intent_sha256s=(_I1, _I2),
        candidate_sha256s=(_A, _B),
        population_id="settled-paper-cohort",
        method="conservative-pairwise-upper-bound",
        sample_size=30,
        causal_cutoff="2026-09-20T00:00:00Z",
        as_of="2026-09-20T00:01:00Z",
        valid_until="2026-09-21T00:01:00Z",
        reproducibility_sha256=_REPRO,
        pairwise_dependency_upper_bounds=pair_values,
        uncertainty_fraction=uncertainty,
        fee_fraction=fee,
        partial_fill_stress_fraction=partial_fill,
    )


def test_dependency_evidence_rejects_mutable_outer_pair_vector() -> None:
    mutable_pairs = [(_A, _B, Decimal("0.9"))]

    with pytest.raises(
        ValueError,
        match="pair bounds must be a canonical tuple",
    ):
        _evidence(pairs=mutable_pairs)


def test_dependency_evidence_rejects_mutable_inner_pair_row() -> None:
    mutable_row = [_A, _B, Decimal("0.9")]

    with pytest.raises(
        ValueError,
        match="pair bound must be a canonical tuple",
    ):
        _evidence(pairs=(mutable_row,))


def test_robust_stress_derive_isolated_from_ambient_decimal_context() -> None:
    evidence = _evidence(
        dependency=Decimal("0.25"),
        uncertainty=Decimal("0.1"),
        fee=Decimal("0.03"),
        partial_fill=Decimal("0.04"),
    )
    base = (Decimal("10.01"), Decimal("20.01"))

    expected = RobustPortfolioProposal.derive(base, evidence)

    with localcontext() as context:
        context.prec = 6
        context.rounding = ROUND_UP
        hostile = RobustPortfolioProposal.derive(base, evidence)

    assert hostile == expected
    assert expected.robust_scale == Decimal("0.6285600")
    assert expected.proposed_stakes == (Decimal("6.29"), Decimal("12.57"))


def test_full_dependency_stress_collapses_joint_vector_to_zero() -> None:
    proposal = RobustPortfolioProposal.derive(
        (Decimal("25"), Decimal("40")),
        _evidence(dependency=Decimal("1")),
    )

    assert proposal.robust_scale == Decimal("0")
    assert proposal.proposed_stakes == (Decimal("0.00"), Decimal("0.00"))


def test_direct_constructor_rejects_inconsistent_scale() -> None:
    with pytest.raises(ValueError, match="scale must exactly match stress factors"):
        RobustPortfolioProposal(
            base_stakes=(Decimal("10"),),
            proposed_stakes=(Decimal("4"),),
            dependency_haircut_fraction=Decimal("0.5"),
            uncertainty_fraction=Decimal("0"),
            fee_fraction=Decimal("0"),
            partial_fill_stress_fraction=Decimal("0"),
            robust_scale=Decimal("0.4"),
        )


def test_serialized_scale_tamper_fails_closed() -> None:
    proposal = RobustPortfolioProposal.derive(
        (Decimal("10"), Decimal("20")),
        _evidence(dependency=Decimal("0.5")),
    )
    payload = proposal.to_dict()
    payload["robust_scale"] = "0.75"

    with pytest.raises(ValueError, match="scale must exactly match stress factors"):
        RobustPortfolioProposal.from_dict(payload)


@pytest.mark.parametrize("schema_version", [True, 1.0])
def test_serialized_schema_version_requires_exact_integer(schema_version: object) -> None:
    proposal = RobustPortfolioProposal.derive(
        (Decimal("10"), Decimal("20")),
        _evidence(),
    )
    payload = proposal.to_dict()
    payload["schema_version"] = schema_version

    with pytest.raises(ValueError, match="unsupported robust proposal schema"):
        RobustPortfolioProposal.from_dict(payload)


def test_serialized_stake_vectors_require_json_lists() -> None:
    proposal = RobustPortfolioProposal.derive(
        (Decimal("10"), Decimal("20")),
        _evidence(),
    )
    payload = proposal.to_dict()
    payload["base_stakes"] = "10"

    with pytest.raises(
        ValueError,
        match="stake vectors must be lists",
    ):
        RobustPortfolioProposal.from_dict(payload)


def test_direct_constructor_rejects_mismatched_stake_cardinality() -> None:
    with pytest.raises(ValueError, match="matching cardinality"):
        RobustPortfolioProposal(
            base_stakes=(Decimal("10"), Decimal("20")),
            proposed_stakes=(Decimal("5"),),
            dependency_haircut_fraction=Decimal("0.5"),
            uncertainty_fraction=Decimal("0"),
            fee_fraction=Decimal("0"),
            partial_fill_stress_fraction=Decimal("0"),
            robust_scale=Decimal("0.5"),
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("dependency_haircut_fraction", Decimal("-0.01")),
        ("uncertainty_fraction", Decimal("1.01")),
        ("fee_fraction", Decimal("NaN")),
        ("partial_fill_stress_fraction", Decimal("Infinity")),
    ],
)
def test_direct_constructor_rejects_invalid_stress_fraction(
    field: str, value: Decimal
) -> None:
    kwargs = {
        "base_stakes": (Decimal("10"),),
        "proposed_stakes": (Decimal("5"),),
        "dependency_haircut_fraction": Decimal("0.5"),
        "uncertainty_fraction": Decimal("0"),
        "fee_fraction": Decimal("0"),
        "partial_fill_stress_fraction": Decimal("0"),
        "robust_scale": Decimal("0.5"),
    }
    kwargs[field] = value

    with pytest.raises(ValueError, match=field):
        RobustPortfolioProposal(**kwargs)

def test_sub_quantum_stress_never_rounds_exposure_up() -> None:
    proposal = RobustPortfolioProposal.derive(
        (Decimal("0.006"), Decimal("0.004")),
        _evidence(dependency=Decimal("0")),
    )

    assert proposal.robust_scale == Decimal("1")
    assert proposal.proposed_stakes == (Decimal("0.00"), Decimal("0.00"))
    assert all(
        proposed <= base
        for proposed, base in zip(
            proposal.proposed_stakes,
            proposal.base_stakes,
            strict=True,
        )
    )


def test_direct_constructor_rejects_stake_above_stressed_base_limit() -> None:
    with pytest.raises(ValueError, match="cannot exceed its conservative stressed base"):
        RobustPortfolioProposal(
            base_stakes=(Decimal("10"),),
            proposed_stakes=(Decimal("9"),),
            dependency_haircut_fraction=Decimal("0.5"),
            uncertainty_fraction=Decimal("0"),
            fee_fraction=Decimal("0"),
            partial_fill_stress_fraction=Decimal("0"),
            robust_scale=Decimal("0.5"),
        )


def test_serialized_stake_above_stressed_base_limit_fails_closed() -> None:
    proposal = RobustPortfolioProposal.derive(
        (Decimal("10"), Decimal("20")),
        _evidence(dependency=Decimal("0.5")),
    )
    payload = proposal.to_dict()
    payload["proposed_stakes"] = ["9", "10"]

    with pytest.raises(ValueError, match="cannot exceed its conservative stressed base"):
        RobustPortfolioProposal.from_dict(payload)


def _three_candidate_evidence(
    bounds: tuple[Decimal, Decimal, Decimal],
) -> PortfolioDependencyEvidence:
    candidate_c = "3" * 64
    intent_c = "c" * 64
    return PortfolioDependencyEvidence(
        evidence_id="dependency-evidence-three-way",
        portfolio_sha256=_PORTFOLIO,
        intent_sha256s=(_I1, _I2, intent_c),
        candidate_sha256s=(_A, _B, candidate_c),
        population_id="settled-paper-cohort",
        method="conservative-pairwise-upper-bound",
        sample_size=30,
        causal_cutoff="2026-09-20T00:00:00Z",
        as_of="2026-09-20T00:01:00Z",
        valid_until="2026-09-21T00:01:00Z",
        reproducibility_sha256=_REPRO,
        pairwise_dependency_upper_bounds=(
            (_A, _B, bounds[0]),
            (_A, candidate_c, bounds[1]),
            (_B, candidate_c, bounds[2]),
        ),
    )


def test_one_high_dependency_edge_dominates_low_dependency_pairs() -> None:
    proposal = RobustPortfolioProposal.derive(
        (Decimal("100"), Decimal("80"), Decimal("60")),
        _three_candidate_evidence(
            (Decimal("0.01"), Decimal("0.90"), Decimal("0.02"))
        ),
    )

    assert proposal.dependency_haircut_fraction == Decimal("0.90")
    assert proposal.robust_scale == Decimal("0.10")
    assert proposal.proposed_stakes == (
        Decimal("10.00"),
        Decimal("8.00"),
        Decimal("6.00"),
    )


def test_non_worst_pair_changes_cannot_relax_worst_pair_haircut() -> None:
    base = (Decimal("100"), Decimal("80"), Decimal("60"))
    sparse = RobustPortfolioProposal.derive(
        base,
        _three_candidate_evidence(
            (Decimal("0.01"), Decimal("0.90"), Decimal("0.02"))
        ),
    )
    dense = RobustPortfolioProposal.derive(
        base,
        _three_candidate_evidence(
            (Decimal("0.89"), Decimal("0.90"), Decimal("0.88"))
        ),
    )

    assert sparse.dependency_haircut_fraction == dense.dependency_haircut_fraction
    assert sparse.robust_scale == dense.robust_scale
    assert sparse.proposed_stakes == dense.proposed_stakes

