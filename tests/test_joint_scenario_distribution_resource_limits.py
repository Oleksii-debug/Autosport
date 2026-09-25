import itertools
import unittest
from decimal import Decimal

from autosport.joint_scenario_distribution import (
    JointScenarioState,
    analyse_joint_distribution,
)
from autosport.paper import PaperBook
from autosport.scenario_search import ScenarioGroup, ScenarioOutcome


class JointScenarioDistributionResourceLimitTests(unittest.TestCase):
    def test_probability_huge_negative_exponent_fails_before_fraction_amplification(self):
        with self.assertRaisesRegex(ValueError, "probability exponent exceeds resource limit"):
            JointScenarioState(
                "tiny-probability",
                ("event|winner|a",),
                Decimal("1E-4097"),
            )

    def test_probability_oversized_coefficient_fails_before_fraction_amplification(self):
        oversized = Decimal((0, (1,) * 4097, -4097))
        with self.assertRaisesRegex(ValueError, "probability coefficient exceeds resource limit"):
            JointScenarioState(
                "oversized-coefficient",
                ("event|winner|a",),
                oversized,
            )

    def test_declared_marginal_probability_shape_is_bounded(self):
        book = PaperBook("100")
        groups = (
            ScenarioGroup(
                "event-winner",
                (
                    ScenarioOutcome("event|winner|a", Decimal("1E-4097")),
                    ScenarioOutcome("event|winner|b", Decimal("0.5")),
                ),
            ),
        )
        with self.assertRaisesRegex(
            ValueError,
            "declared marginal probability exponent exceeds resource limit",
        ):
            analyse_joint_distribution(book, groups, ())

    def test_infinite_group_iterable_is_cut_off_by_resource_limit(self):
        book = PaperBook("100")
        group = ScenarioGroup(
            "event-winner",
            (
                ScenarioOutcome("event|winner|a", Decimal("0.5")),
                ScenarioOutcome("event|winner|b", Decimal("0.5")),
            ),
        )
        with self.assertRaisesRegex(ValueError, "joint scenario groups exceeds resource limit"):
            analyse_joint_distribution(book, itertools.repeat(group), ())

    def test_infinite_state_iterable_is_cut_off_by_resource_limit(self):
        book = PaperBook("100")
        groups = (
            ScenarioGroup(
                "event-winner",
                (
                    ScenarioOutcome("event|winner|a", Decimal("0.5")),
                    ScenarioOutcome("event|winner|b", Decimal("0.5")),
                ),
            ),
        )
        with self.assertRaisesRegex(ValueError, "joint scenario states exceeds resource limit"):
            analyse_joint_distribution(book, groups, itertools.repeat(object()))

    def test_state_selected_key_count_is_bounded_before_sorting_or_set_build(self):
        with self.assertRaisesRegex(
            ValueError,
            "selected_quote_keys exceeds resource limit",
        ):
            JointScenarioState(
                "too-many-keys",
                tuple(f"event-{index}|winner|a" for index in range(257)),
                Decimal("1"),
            )


if __name__ == "__main__":
    unittest.main()
