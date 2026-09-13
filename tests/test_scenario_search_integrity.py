import unittest
from decimal import Decimal

from autosport.domain import TicketLeg
from autosport.paper import PaperBook
from autosport.scenario_search import ScenarioGroup, ScenarioOutcome, ScenarioSearchEngine


class ScenarioSearchIntegrityTests(unittest.TestCase):
    def test_engine_rejects_invalid_search_limits(self) -> None:
        cases = (
            ("exact_state_limit", 0),
            ("exact_state_limit", -1),
            ("exact_state_limit", True),
            ("exact_state_limit", "10"),
            ("branch_node_limit", 0),
            ("branch_node_limit", -1),
            ("branch_node_limit", False),
            ("branch_node_limit", "10"),
            ("sample_count", 0),
            ("sample_count", -1),
            ("sample_count", True),
            ("sample_count", "10"),
        )
        for field, value in cases:
            with self.subTest(field=field, value=value):
                with self.assertRaisesRegex(ValueError, rf"{field} must be a positive integer"):
                    ScenarioSearchEngine(**{field: value})

    def test_engine_rejects_non_integer_or_boolean_seed(self) -> None:
        for value in (True, False, "17", Decimal("17"), 17.0):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "seed must be an integer"):
                    ScenarioSearchEngine(seed=value)

    def test_group_rejects_non_decimal_or_non_finite_probabilities(self) -> None:
        for value in ("0.5", Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "outcome probability"):
                    ScenarioGroup(
                        "event-winner",
                        (
                            ScenarioOutcome("event|winner|a", value),
                            ScenarioOutcome("event|winner|b", Decimal("0.5")),
                        ),
                    )

    def test_valid_probability_boundaries_remain_supported(self) -> None:
        group = ScenarioGroup(
            "event-winner",
            (
                ScenarioOutcome("event|winner|a", Decimal("0")),
                ScenarioOutcome("event|winner|b", Decimal("1")),
            ),
        )
        self.assertEqual(group.outcomes[0].probability, Decimal("0"))
        self.assertEqual(group.outcomes[1].probability, Decimal("1"))

    def test_bounded_sampling_is_stable_across_equivalent_group_and_outcome_order(self) -> None:
        book = PaperBook("1000")
        a1 = TicketLeg("event-a", "winner", "a1", Decimal("2"))
        a2 = TicketLeg("event-a", "winner", "a2", Decimal("7"))
        b1 = TicketLeg("event-b", "winner", "b1", Decimal("3"))
        b2 = TicketLeg("event-b", "winner", "b2", Decimal("11"))
        tickets = [
            book.open_ticket([a1], "3"),
            book.open_ticket([a2], "2"),
            book.open_ticket([b1], "5"),
            book.open_ticket([b2], "1"),
        ]
        group_a = ScenarioGroup(
            "event-a-winner",
            (ScenarioOutcome(a1.quote_key), ScenarioOutcome(a2.quote_key)),
        )
        group_a_reversed = ScenarioGroup(
            "event-a-winner",
            (ScenarioOutcome(a2.quote_key), ScenarioOutcome(a1.quote_key)),
        )
        group_b = ScenarioGroup(
            "event-b-winner",
            (ScenarioOutcome(b1.quote_key), ScenarioOutcome(b2.quote_key)),
        )
        group_b_reversed = ScenarioGroup(
            "event-b-winner",
            (ScenarioOutcome(b2.quote_key), ScenarioOutcome(b1.quote_key)),
        )
        engine = ScenarioSearchEngine(
            exact_state_limit=1,
            branch_node_limit=1,
            sample_count=31,
            seed=19,
        )

        forward = engine.analyse(tickets, [group_a, group_b])
        reordered = engine.analyse(tickets, [group_b_reversed, group_a_reversed])

        self.assertEqual(forward, reordered)
        self.assertEqual(forward.mode, "bounded-approximation")
        self.assertEqual(forward.nodes_explored, 33)


if __name__ == "__main__":
    unittest.main()
