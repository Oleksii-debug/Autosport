from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
from math import comb

from autosport.risk_of_ruin_evaluator import clopper_pearson_upper_bound


def _exact_binomial_cdf(
    *,
    successes_at_most: int,
    trials: int,
    probability: Fraction,
) -> Fraction:
    complement = Fraction(1) - probability
    return sum(
        Fraction(comb(trials, successes))
        * probability**successes
        * complement ** (trials - successes)
        for successes in range(successes_at_most + 1)
    )


def test_clopper_pearson_upper_bound_is_outward_conservative_after_serializable_rounding() -> None:
    confidence = Decimal("0.95")
    upper = clopper_pearson_upper_bound(
        ruin_count=0,
        independent_units=3,
        confidence_level=confidence,
    )

    # For the one-sided Clopper-Pearson upper endpoint U, the exact binomial
    # lower-tail probability at U must be <= alpha.  If it is greater than
    # alpha, U lies below the mathematical root and is not an upper bound.
    exact_cdf = _exact_binomial_cdf(
        successes_at_most=0,
        trials=3,
        probability=Fraction(upper),
    )
    alpha = Fraction(Decimal(1) - confidence)

    assert exact_cdf <= alpha, (
        "returned Clopper-Pearson endpoint rounded below the exact upper root; "
        f"exact CDF(U)-alpha={exact_cdf - alpha}"
    )
