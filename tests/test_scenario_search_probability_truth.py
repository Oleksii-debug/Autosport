from __future__ import annotations

import unittest
from decimal import Decimal, localcontext

from autosport.domain import TicketLeg
from autosport.paper import PaperBook
from autosport.scenario_search import (
    ScenarioGroup,
    ScenarioOutcome,
    ScenarioSearchEngine,
    _integer_probability_weights,
    _weighted_choice,
)


class ScenarioSearchProbabilityTruthTests(unittest.TestCase):
    def test_probability_values_require_exact_finite_decimal(self) -> None:
        for value in (True, False, 1, 0, 0.5, Decimal("NaN"), Decimal("Infinity")):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    ValueError,
                    "scenario outcome probability must be an exact finite Decimal",
                ):
                    ScenarioOutcome("event|winner|a", value)  # type: ignore[arg-type]

    def test_probability_mass_must_sum_exactly_to_one(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "scenario group probabilities must sum exactly to 1",
        ):
            ScenarioGroup(
                "event-winner",
                (
                    ScenarioOutcome("event|winner|a", Decimal("0.5000000004")),
                    ScenarioOutcome("event|winner|b", Decimal("0.5")),
                ),
            )

    def test_probability_shape_resource_bounds_fail_closed(self) -> None:
        oversized_exponent = Decimal((0, (1,), -4097))
        oversized_coefficient = Decimal((0, (1,) * 4097, -4097))
        for value, reason in (
            (oversized_exponent, "exponent exceeds resource limit"),
            (oversized_coefficient, "coefficient exceeds resource limit"),
        ):
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(ValueError, reason):
                    ScenarioOutcome("event|winner|a", value)

    def test_group_and_outcome_cardinality_resource_bounds_fail_closed(self) -> None:
        oversized_outcomes = tuple(
            ScenarioOutcome(f"event|winner|selection-{index}")
            for index in range(1025)
        )
        with self.assertRaisesRegex(ValueError, "scenario group outcomes exceeds resource limit"):
            ScenarioGroup("oversized-group", oversized_outcomes)

        groups = [
            ScenarioGroup(
                f"group-{index:03d}",
                (
                    ScenarioOutcome(f"event-{index}|winner|a"),
                    ScenarioOutcome(f"event-{index}|winner|b"),
                ),
            )
            for index in range(257)
        ]
        with self.assertRaisesRegex(ValueError, "scenario groups exceeds resource limit"):
            ScenarioSearchEngine().analyse([], groups)

    def test_duplicate_group_id_fails_closed_before_analysis(self) -> None:
        first = ScenarioGroup(
            "duplicate-id",
            (
                ScenarioOutcome("event-1|winner|a"),
                ScenarioOutcome("event-1|winner|b"),
            ),
        )
        second = ScenarioGroup(
            "duplicate-id",
            (
                ScenarioOutcome("event-2|winner|a"),
                ScenarioOutcome("event-2|winner|b"),
            ),
        )
        with self.assertRaisesRegex(ValueError, "scenario group_id values must be unique"):
            ScenarioSearchEngine().analyse([], [first, second])

    def test_probability_mass_exact_decimal_equivalence_is_accepted(self) -> None:
        group = ScenarioGroup(
            "event-winner",
            (
                ScenarioOutcome("event|winner|a", Decimal("0.60")),
                ScenarioOutcome("event|winner|b", Decimal("0.400")),
            ),
        )
        self.assertEqual(
            tuple(outcome.probability for outcome in group.outcomes),
            (Decimal("0.60"), Decimal("0.400")),
        )

    def test_group_boundary_rejects_noncanonical_identity_and_members(self) -> None:
        with self.assertRaisesRegex(ValueError, "scenario group_id"):
            ScenarioGroup(
                " event-winner",
                (
                    ScenarioOutcome("event|winner|a"),
                    ScenarioOutcome("event|winner|b"),
                ),
            )
        with self.assertRaisesRegex(ValueError, "must be a tuple"):
            ScenarioGroup(  # type: ignore[arg-type]
                "event-winner",
                [
                    ScenarioOutcome("event|winner|a"),
                    ScenarioOutcome("event|winner|b"),
                ],
            )

    def test_exact_expected_value_is_independent_of_ambient_decimal_context(self) -> None:
        book = PaperBook("1000")
        a = TicketLeg("e1", "winner", "a", Decimal("2"))
        b = TicketLeg("e1", "winner", "b", Decimal("3"))
        ticket_a = book.open_ticket([a], "10")
        ticket_b = book.open_ticket([b], "10")
        groups = [
            ScenarioGroup(
                "e1-winner",
                (
                    ScenarioOutcome(a.quote_key, Decimal("0.6")),
                    ScenarioOutcome(b.quote_key, Decimal("0.4")),
                ),
            )
        ]

        baseline = ScenarioSearchEngine().analyse([ticket_a, ticket_b], groups)
        with localcontext() as context:
            context.prec = 2
            hostile = ScenarioSearchEngine().analyse([ticket_a, ticket_b], groups)

        self.assertEqual(baseline.expected_case, Decimal("4.0"))
        self.assertEqual(hostile.expected_case, baseline.expected_case)
        self.assertEqual(hostile.expected_mode, "exact-independent-groups")

    def test_sampling_is_invariant_to_semantically_equivalent_group_order(self) -> None:
        book = PaperBook("1000")
        a1 = TicketLeg("e1", "winner", "a", Decimal("2"))
        b1 = TicketLeg("e1", "winner", "b", Decimal("3"))
        a2 = TicketLeg("e2", "winner", "a", Decimal("2.5"))
        b2 = TicketLeg("e2", "winner", "b", Decimal("1.8"))
        tickets = [
            book.open_ticket([a1], "10"),
            book.open_ticket([b1], "7"),
            book.open_ticket([a2], "6"),
            book.open_ticket([b2], "5"),
        ]
        first = ScenarioGroup(
            "e1-winner",
            (
                ScenarioOutcome(a1.quote_key, Decimal("0.6")),
                ScenarioOutcome(b1.quote_key, Decimal("0.4")),
            ),
        )
        second = ScenarioGroup(
            "e2-winner",
            (
                ScenarioOutcome(a2.quote_key, Decimal("0.3")),
                ScenarioOutcome(b2.quote_key, Decimal("0.7")),
            ),
        )
        reversed_first = ScenarioGroup("e1-winner", tuple(reversed(first.outcomes)))
        reversed_second = ScenarioGroup("e2-winner", tuple(reversed(second.outcomes)))
        engine = ScenarioSearchEngine(
            exact_state_limit=1,
            branch_node_limit=1,
            sample_count=257,
            seed=41,
        )

        left = engine.analyse(tickets, [first, second])
        right = engine.analyse(tickets, [reversed_second, reversed_first])

        self.assertEqual(left, right)
        self.assertEqual(left.mode, "bounded-approximation")
        self.assertEqual(left.expected_mode, "sampled-independent-groups")

    def test_zero_probability_outcome_cannot_be_selected_at_zero_target(self) -> None:
        outcomes = (
            ScenarioOutcome("event|winner|never", Decimal("0")),
            ScenarioOutcome("event|winner|certain", Decimal("1")),
        )
        weights = _integer_probability_weights(outcomes)

        class ZeroTargetRng:
            @staticmethod
            def randrange(stop: int) -> int:
                if stop <= 0:
                    raise AssertionError("stop must be positive")
                return 0

        selected = _weighted_choice(ZeroTargetRng(), outcomes, weights)  # type: ignore[arg-type]
        self.assertEqual(selected.quote_key, "event|winner|certain")


if __name__ == "__main__":
    unittest.main()
