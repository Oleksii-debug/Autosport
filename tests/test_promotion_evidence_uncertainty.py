from decimal import Decimal

import pytest

from autosport._strategy_model_factory_impl import _promotion_effect_interval


def test_promotion_interval_requires_frozen_method_to_match_implemented_procedure():
    paired = (Decimal("0.1"), Decimal("0.2"), Decimal("0.15"))

    method, low, high = _promotion_effect_interval(
        paired,
        "paired min/max interval",
    )

    assert method == "paired min/max interval"
    assert low == Decimal("0.1")
    assert high == Decimal("0.2")


@pytest.mark.parametrize("declared_method", ["bootstrap intervals", "normal approximation"])
def test_promotion_interval_rejects_declared_inferential_method_without_estimator(
    declared_method,
):
    with pytest.raises(ValueError, match="unsupported frozen uncertainty_method"):
        _promotion_effect_interval((Decimal("0.1"), Decimal("0.2")), declared_method)
