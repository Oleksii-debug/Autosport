from __future__ import annotations

import math
import unittest
from decimal import Decimal

from autosport.forecasting import _binary_log_loss


class ForecastLogLossDecimalPrecisionFalsifierTests(unittest.TestCase):
    def test_near_one_probability_uses_declared_decimal_complement(self) -> None:
        probability = Decimal("0.9999999999999999")

        actual = _binary_log_loss(probability, 0)
        expected = -math.log(1e-16)

        self.assertAlmostEqual(actual, expected, places=12)


if __name__ == "__main__":
    unittest.main()
