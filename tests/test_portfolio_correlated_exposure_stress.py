from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.portfolio_plan import PortfolioDependencyEvidence, RobustPortfolioProposal


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
) -> PortfolioDependencyEvidence:
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
        pairwise_dependency_upper_bounds=((_A, _B, dependency),),
        uncertainty_fraction=uncertainty,
        fee_fraction=fee,
        partial_fill_stress_fraction=partial_fill,
    )


def test_robust_proposal_rounding_never_increases_sub_quantum_stake() -> None:
    proposal = RobustPortfolioProposal.derive(
        (Decimal("0.006"), Decimal("10")),
        _evidence(dependency=Decimal("0")),
    )

    assert proposal.robust_scale == Decimal("1")
    assert proposal.proposed_stakes == (Decimal("0.00"), Decimal("10.00"))
    assert all(
        proposed <= base
        for base, proposed in zip(
            proposal.base_stakes, proposal.proposed_stakes, strict=True
        )
    )


def test_full_dependency_stress_collapses_joint_vector_to_zero() -> None:
    proposal = RobustPortfolioProposal.derive(
        (Decimal("25"), Decimal("40")),
        _evidence(dependency=Decimal("1")),
    )

    assert proposal.robust_scale == Decimal("0")
    assert proposal.proposed_stakes == (Decimal("0.00"), Decimal("0.00"))


def test_direct_constructor_rejects_stake_above_stressed_exposure() -> None:
    with pytest.raises(
        ValueError,
        match="must not increase stake above stressed base exposure",
    ):
        RobustPortfolioProposal(
            base_stakes=(Decimal("10"),),
            proposed_stakes=(Decimal("6"),),
            dependency_haircut_fraction=Decimal("0.5"),
            uncertainty_fraction=Decimal("0"),
            fee_fraction=Decimal("0"),
            partial_fill_stress_fraction=Decimal("0"),
            robust_scale=Decimal("0.5"),
        )


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


def test_serialized_tamper_cannot_restore_pre_haircut_stake() -> None:
    proposal = RobustPortfolioProposal.derive(
        (Decimal("10"), Decimal("20")),
        _evidence(dependency=Decimal("0.5")),
    )
    payload = proposal.to_dict()
    payload["proposed_stakes"][0] = "9"

    with pytest.raises(
        ValueError,
        match="must not increase stake above stressed base exposure",
    ):
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
