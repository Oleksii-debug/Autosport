from __future__ import annotations

from decimal import Decimal, ROUND_UP, localcontext

from autosport.portfolio_plan import PortfolioDependencyEvidence, RobustPortfolioProposal


_A = "1" * 64
_B = "2" * 64
_I1 = "a" * 64
_I2 = "b" * 64
_PORTFOLIO = "c" * 64
_REPRO = "d" * 64


def _evidence(*, dependency: Decimal = Decimal("0")) -> PortfolioDependencyEvidence:
    return PortfolioDependencyEvidence(
        evidence_id="dependency-evidence-quantum-grid",
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
    )


def test_non_power_of_ten_quantum_is_an_exact_money_grid() -> None:
    proposal = RobustPortfolioProposal.derive(
        (Decimal("10.03"), Decimal("20.08")),
        _evidence(),
        quantum=Decimal("0.05"),
    )

    assert proposal.proposed_stakes == (Decimal("10.00"), Decimal("20.05"))
    assert all(stake % Decimal("0.05") == 0 for stake in proposal.proposed_stakes)
    assert all(
        proposed <= base
        for proposed, base in zip(
            proposal.proposed_stakes,
            proposal.base_stakes,
            strict=True,
        )
    )


def test_arbitrary_quantum_floors_stressed_value_not_only_decimal_exponent() -> None:
    proposal = RobustPortfolioProposal.derive(
        (Decimal("10.01"), Decimal("7.99")),
        _evidence(dependency=Decimal("0.25")),
        quantum=Decimal("0.05"),
    )

    # Exact stressed values are 7.5075 and 5.9925.  Exponent-only quantize
    # would produce 7.51 / 5.99; the money grid requires 7.50 / 5.95.
    assert proposal.proposed_stakes == (Decimal("7.50"), Decimal("5.95"))


def test_quantum_grid_isolated_from_hostile_ambient_decimal_context() -> None:
    expected = RobustPortfolioProposal.derive(
        (Decimal("123.4567"), Decimal("9.8765")),
        _evidence(dependency=Decimal("0.13")),
        quantum=Decimal("0.03"),
    )

    with localcontext() as context:
        context.prec = 6
        context.rounding = ROUND_UP
        hostile = RobustPortfolioProposal.derive(
            (Decimal("123.4567"), Decimal("9.8765")),
            _evidence(dependency=Decimal("0.13")),
            quantum=Decimal("0.03"),
        )

    assert hostile == expected
    assert all(stake % Decimal("0.03") == 0 for stake in hostile.proposed_stakes)


def test_quantum_larger_than_stressed_stake_conservatively_returns_zero() -> None:
    proposal = RobustPortfolioProposal.derive(
        (Decimal("0.04"), Decimal("0.09")),
        _evidence(),
        quantum=Decimal("0.05"),
    )

    assert proposal.proposed_stakes == (Decimal("0.00"), Decimal("0.05"))
