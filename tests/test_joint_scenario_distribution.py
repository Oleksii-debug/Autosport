import unittest
from decimal import Decimal, localcontext
from unittest.mock import patch

from autosport.domain import TicketLeg
from autosport.joint_scenario_distribution import (
    JointScenarioReport,
    JointScenarioState,
    analyse_joint_distribution,
)
from autosport.paper import PaperBook
from autosport.portfolio import PortfolioEngine
from autosport.scenario_search import ScenarioGroup, ScenarioOutcome, ScenarioSearchEngine


class JointScenarioDistributionTests(unittest.TestCase):
    def _fixture(self):
        book = PaperBook("100")
        a1 = TicketLeg("e1", "winner", "a", Decimal("2"))
        b1 = TicketLeg("e1", "winner", "b", Decimal("2"))
        a2 = TicketLeg("e2", "winner", "a", Decimal("2"))
        b2 = TicketLeg("e2", "winner", "b", Decimal("2"))
        ticket = book.open_ticket([a1, a2], "10")
        groups = (
            ScenarioGroup(
                "e1-winner",
                (
                    ScenarioOutcome(a1.quote_key, Decimal("0.5")),
                    ScenarioOutcome(b1.quote_key, Decimal("0.5")),
                ),
            ),
            ScenarioGroup(
                "e2-winner",
                (
                    ScenarioOutcome(a2.quote_key, Decimal("0.5")),
                    ScenarioOutcome(b2.quote_key, Decimal("0.5")),
                ),
            ),
        )
        return book, ticket, groups, (a1, b1, a2, b2)

    def test_explicit_positive_dependence_differs_from_independent_expected_value(self):
        book, ticket, groups, (a1, b1, a2, b2) = self._fixture()
        independent = ScenarioSearchEngine().analyse([ticket], list(groups))
        self.assertEqual(independent.expected_mode, "exact-independent-groups")
        self.assertEqual(independent.expected_case, Decimal("0"))

        report = analyse_joint_distribution(
            book,
            groups,
            (
                JointScenarioState(
                    "both-a",
                    (a1.quote_key, a2.quote_key),
                    Decimal("0.5"),
                ),
                JointScenarioState(
                    "both-b",
                    (b1.quote_key, b2.quote_key),
                    Decimal("0.5"),
                ),
            ),
        )

        self.assertEqual(report.mode, "explicit-joint-distribution")
        self.assertEqual(report.scenario_count, 2)
        self.assertEqual(report.observed_worst, Decimal("-10"))
        self.assertEqual(report.observed_best, Decimal("30"))
        self.assertEqual(report.expected_case, Decimal("10"))
        self.assertFalse(report.probability_authority_proven)
        self.assertEqual(len(report.portfolio_sha256), 64)
        self.assertEqual(len(report.distribution_sha256), 64)

    def test_explicit_negative_dependence_can_make_parlay_always_lose(self):
        book, _ticket, groups, (a1, b1, a2, b2) = self._fixture()

        report = analyse_joint_distribution(
            book,
            groups,
            (
                JointScenarioState(
                    "a1-b2",
                    (a1.quote_key, b2.quote_key),
                    Decimal("0.5"),
                ),
                JointScenarioState(
                    "b1-a2",
                    (b1.quote_key, a2.quote_key),
                    Decimal("0.5"),
                ),
            ),
        )

        self.assertEqual(report.observed_worst, Decimal("-10"))
        self.assertEqual(report.observed_best, Decimal("-10"))
        self.assertEqual(report.expected_case, Decimal("-10"))

    def test_distribution_digest_is_order_and_decimal_representation_stable(self):
        book, _ticket, groups, (a1, b1, a2, b2) = self._fixture()
        first = analyse_joint_distribution(
            book,
            groups,
            (
                JointScenarioState(
                    "both-a",
                    (a1.quote_key, a2.quote_key),
                    Decimal("0.50"),
                ),
                JointScenarioState(
                    "both-b",
                    (b1.quote_key, b2.quote_key),
                    Decimal("0.50"),
                ),
            ),
        )
        second = analyse_joint_distribution(
            book,
            tuple(reversed(groups)),
            (
                JointScenarioState(
                    "both-b",
                    (b2.quote_key, b1.quote_key),
                    Decimal("0.5"),
                ),
                JointScenarioState(
                    "both-a",
                    (a2.quote_key, a1.quote_key),
                    Decimal("0.5"),
                ),
            ),
        )
        self.assertEqual(first.distribution_sha256, second.distribution_sha256)

    def test_state_must_select_exactly_one_outcome_from_every_group(self):
        book, _ticket, groups, (a1, _b1, a2, b2) = self._fixture()
        with self.assertRaisesRegex(
            ValueError,
            "exactly one outcome from every group",
        ):
            analyse_joint_distribution(
                book,
                groups,
                (
                    JointScenarioState(
                        "invalid",
                        (a1.quote_key, a2.quote_key, b2.quote_key),
                        Decimal("1"),
                    ),
                ),
            )

    def test_duplicate_assignment_is_rejected_even_with_different_state_ids(self):
        book, _ticket, groups, (a1, _b1, a2, _b2) = self._fixture()
        with self.assertRaisesRegex(ValueError, "duplicate an outcome assignment"):
            analyse_joint_distribution(
                book,
                groups,
                (
                    JointScenarioState(
                        "first",
                        (a1.quote_key, a2.quote_key),
                        Decimal("0.5"),
                    ),
                    JointScenarioState(
                        "same-assignment",
                        (a2.quote_key, a1.quote_key),
                        Decimal("0.5"),
                    ),
                ),
            )

    def test_total_probability_must_equal_one_exactly(self):
        book, _ticket, groups, (a1, b1, a2, b2) = self._fixture()
        with self.assertRaisesRegex(ValueError, "sum exactly to 1"):
            analyse_joint_distribution(
                book,
                groups,
                (
                    JointScenarioState(
                        "both-a",
                        (a1.quote_key, a2.quote_key),
                        Decimal("0.4"),
                    ),
                    JointScenarioState(
                        "both-b",
                        (b1.quote_key, b2.quote_key),
                        Decimal("0.5"),
                    ),
                ),
            )

    def test_declared_marginal_contradiction_fails_closed(self):
        book, _ticket, groups, (a1, b1, a2, b2) = self._fixture()
        with self.assertRaisesRegex(ValueError, "contradicts declared marginal"):
            analyse_joint_distribution(
                book,
                groups,
                (
                    JointScenarioState(
                        "both-a",
                        (a1.quote_key, a2.quote_key),
                        Decimal("0.75"),
                    ),
                    JointScenarioState(
                        "both-b",
                        (b1.quote_key, b2.quote_key),
                        Decimal("0.25"),
                    ),
                ),
            )

    def test_zero_probability_state_is_rejected_instead_of_hiding_dead_states(self):
        _book, _ticket, _groups, (a1, _b1, a2, _b2) = self._fixture()
        with self.assertRaisesRegex(ValueError, "positive finite Decimal"):
            JointScenarioState(
                "zero",
                (a1.quote_key, a2.quote_key),
                Decimal("0"),
            )

    def test_expected_value_is_independent_of_callers_decimal_context(self):
        book, _ticket, groups, (a1, b1, a2, b2) = self._fixture()
        states = (
            JointScenarioState(
                "both-a",
                (a1.quote_key, a2.quote_key),
                Decimal("0.5"),
            ),
            JointScenarioState(
                "both-b",
                (b1.quote_key, b2.quote_key),
                Decimal("0.5"),
            ),
        )
        with localcontext() as context:
            context.prec = 3
            low_precision = analyse_joint_distribution(book, groups, states)
        with localcontext() as context:
            context.prec = 50
            high_precision = analyse_joint_distribution(book, groups, states)

        self.assertEqual(low_precision, high_precision)

    def test_book_mutation_during_analysis_fails_closed(self):
        book, _ticket, groups, (a1, b1, a2, b2) = self._fixture()
        original = PortfolioEngine.scenario_profit
        calls = 0

        def mutating_profit(tickets, winning_quote_keys):
            nonlocal calls
            calls += 1
            result = original(tickets, winning_quote_keys)
            if calls == 1:
                book.open_ticket(
                    [TicketLeg("e3", "winner", "x", Decimal("2"))],
                    "1",
                )
            return result

        with patch.object(PortfolioEngine, "scenario_profit", side_effect=mutating_profit):
            with self.assertRaisesRegex(ValueError, "changed during joint scenario analysis"):
                analyse_joint_distribution(
                    book,
                    groups,
                    (
                        JointScenarioState(
                            "both-a",
                            (a1.quote_key, a2.quote_key),
                            Decimal("0.5"),
                        ),
                        JointScenarioState(
                            "both-b",
                            (b1.quote_key, b2.quote_key),
                            Decimal("0.5"),
                        ),
                    ),
                )

    def test_probability_authority_flag_cannot_be_minted_by_report_constructor(self):
        with self.assertRaises(TypeError):
            JointScenarioReport(
                mode="forged",
                scenario_count=1,
                observed_worst=Decimal("0"),
                observed_best=Decimal("0"),
                expected_case=Decimal("0"),
                portfolio_sha256="0" * 64,
                distribution_sha256="1" * 64,
                probability_authority_proven=True,
            )


if __name__ == "__main__":
    unittest.main()
