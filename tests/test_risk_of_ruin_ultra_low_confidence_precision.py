from decimal import Decimal
from fractions import Fraction

import pytest

from autosport.risk_of_ruin_evaluator import clopper_pearson_upper_bound


@pytest.mark.parametrize(
    "confidence",
    (
        Decimal("1E-69"),
        Decimal("1E-70"),
        Decimal("1E-71"),
    ),
)
def test_ultra_low_exact_confidence_never_moves_clopper_pearson_upper_bound_inward(
    confidence: Decimal,
) -> None:
    """For n=1,k=0 the exact one-sided CP upper endpoint is confidence itself.

    The public contract accepts any exact finite Decimal strictly inside (0, 1).
    Therefore internal working precision must not round 1-confidence so coarsely
    that the returned endpoint falls below the exact confidence value.
    """

    upper = clopper_pearson_upper_bound(
        ruin_count=0,
        independent_units=1,
        confidence_level=confidence,
    )

    # Closed-form oracle for X~Binomial(1,p), k=0:
    #   P_p(X <= 0) = 1-p <= alpha = 1-confidence
    # iff p >= confidence.
    assert upper >= confidence

    # Preserve the same invariant with exact rational arithmetic so this
    # regression cannot be hidden by Decimal context/rounding behavior.
    exact_cdf_at_upper = Fraction(1) - Fraction(upper)
    exact_alpha = Fraction(1) - Fraction(confidence)
    assert exact_cdf_at_upper <= exact_alpha
