from __future__ import annotations

import math
import unittest
from decimal import Decimal

from autosport.domain import MarketEvent
from autosport.probability import (
    ForecastObservation,
    implied_probability,
    log_loss,
    normalize_two_or_more_way_market,
    paper_value,
)


class ProbabilityNonFiniteIntegrityTests(unittest.TestCase):
    def test_implied_probability_rejects_nonfinite_decimal_odds(self) -> None:
        for value in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "decimal odds must be finite"):
                    implied_probability(value)

    def test_paper_value_rejects_nonfinite_probability(self) -> None:
        for value in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "probability must be finite"):
                    paper_value("quote", value, "2.0")

    def test_paper_value_rejects_nonfinite_decimal_odds(self) -> None:
        for value in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "decimal odds must be finite"):
                    paper_value("quote", "0.5", value)

    def test_normalization_rejects_duplicate_selection(self) -> None:
        first = MarketEvent.from_dict(
            {
                "event_id": "e",
                "market_id": "m",
                "selection_id": "a",
                "decimal_odds": "2.0",
                "observed_ts": "2026-01-01T00:00:00+00:00",
                "source_id": "s",
                "sequence": 1,
            }
        )
        duplicate = MarketEvent.from_dict(
            {
                "event_id": "e",
                "market_id": "m",
                "selection_id": "a",
                "decimal_odds": "2.2",
                "observed_ts": "2026-01-01T00:00:01+00:00",
                "source_id": "s",
                "sequence": 2,
            }
        )
        with self.assertRaisesRegex(ValueError, "distinct selections"):
            normalize_two_or_more_way_market([first, duplicate])

    def test_log_loss_rejects_nonfinite_and_out_of_range_epsilon(self) -> None:
        observations = [ForecastObservation(0.5, 1)]
        for epsilon in (
            float("nan"),
            float("inf"),
            float("-inf"),
            0.0,
            -1.0,
            0.5,
            1.0,
            True,
            10**10000,
        ):
            with self.subTest(epsilon=epsilon):
                with self.assertRaisesRegex(ValueError, "epsilon must be a finite number"):
                    log_loss(observations, epsilon=epsilon)

    def test_log_loss_rejects_epsilon_that_cannot_move_upper_endpoint(self) -> None:
        epsilon = math.nextafter(0.0, 1.0)
        self.assertGreater(epsilon, 0.0)
        self.assertEqual(1.0 - epsilon, 1.0)
        with self.assertRaisesRegex(ValueError, "epsilon must be a finite number"):
            log_loss([ForecastObservation(1.0, 0)], epsilon=epsilon)

    def test_finite_probability_economics_remain_unchanged(self) -> None:
        self.assertEqual(implied_probability(Decimal("2.0")), Decimal("0.5"))
        estimate = paper_value("quote", Decimal("0.60"), Decimal("2.0"))
        self.assertEqual(estimate.expected_profit_per_unit, Decimal("0.200"))
        loss = log_loss([ForecastObservation(0.8, 1), ForecastObservation(0.2, 0)])
        self.assertTrue(math.isfinite(loss))
        self.assertGreater(loss, 0.0)


if __name__ == "__main__":
    unittest.main()
