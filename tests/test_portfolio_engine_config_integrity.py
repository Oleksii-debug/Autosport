import unittest
from decimal import Decimal

from autosport.domain import TicketLeg
from autosport.paper import PaperBook
from autosport.portfolio import PortfolioEngine


class PortfolioEngineConfigurationIntegrityTests(unittest.TestCase):
    def test_engine_rejects_invalid_enumeration_and_sampling_limits(self) -> None:
        cases = (
            ("max_exact_states", 0),
            ("max_exact_states", -1),
            ("max_exact_states", True),
            ("max_exact_states", "10"),
            ("sample_count", 0),
            ("sample_count", -1),
            ("sample_count", False),
            ("sample_count", "10"),
        )
        for field, value in cases:
            with self.subTest(field=field, value=value):
                with self.assertRaisesRegex(ValueError, rf"{field} must be a positive integer"):
                    PortfolioEngine(**{field: value})

    def test_engine_rejects_non_integer_or_boolean_seed(self) -> None:
        for value in (True, False, "7", Decimal("7"), 7.0):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "seed must be an integer"):
                    PortfolioEngine(seed=value)

    def test_empty_exclusive_group_fails_before_scenario_enumeration(self) -> None:
        book = PaperBook("100")
        leg = TicketLeg("event", "winner", "alice", Decimal("2"))
        ticket = book.open_ticket([leg], "10")

        with self.assertRaisesRegex(ValueError, "exclusive groups must not be empty"):
            PortfolioEngine().analyse([ticket], exclusive_groups=[set()])

    def test_empty_exclusive_group_fails_for_empty_portfolio(self) -> None:
        with self.assertRaisesRegex(ValueError, "exclusive groups must not be empty"):
            PortfolioEngine().analyse([], exclusive_groups=[set()])

    def test_absent_exclusive_group_fails_for_empty_portfolio(self) -> None:
        with self.assertRaisesRegex(ValueError, "exclusive group contains quote not present in portfolio"):
            PortfolioEngine().analyse([], exclusive_groups=[{"ghost|winner|x"}])

    def test_absent_exclusive_group_fails_for_closed_only_portfolio(self) -> None:
        book = PaperBook("100")
        leg = TicketLeg("event", "winner", "alice", Decimal("2"))
        ticket = book.open_ticket([leg], "10")
        book.settle(ticket.ticket_id, {leg.quote_key})

        with self.assertRaisesRegex(ValueError, "exclusive group contains quote not present in portfolio"):
            PortfolioEngine().analyse([ticket], exclusive_groups=[{leg.quote_key}])

    def test_valid_empty_portfolio_keeps_zero_exact_report(self) -> None:
        report = PortfolioEngine().analyse([])
        self.assertEqual(report.mode, "exact")
        self.assertEqual(report.scenario_count, 1)
        self.assertEqual(report.worst_case, Decimal("0"))
        self.assertEqual(report.best_case, Decimal("0"))
        self.assertEqual(report.mean_case, Decimal("0"))

    def test_valid_approximate_configuration_produces_requested_finite_samples(self) -> None:
        book = PaperBook("100")
        alice = TicketLeg("event", "winner", "alice", Decimal("2"))
        bob = TicketLeg("event", "winner", "bob", Decimal("3"))
        tickets = [book.open_ticket([alice], "10"), book.open_ticket([bob], "10")]

        report = PortfolioEngine(max_exact_states=1, sample_count=7, seed=11).analyse(tickets)

        self.assertEqual(report.mode, "approximate")
        self.assertEqual(report.scenario_count, 7)
        self.assertTrue(report.worst_case.is_finite())
        self.assertTrue(report.best_case.is_finite())
        self.assertTrue(report.mean_case.is_finite())

    def test_approximate_sampling_is_stable_across_equivalent_group_order(self) -> None:
        book = PaperBook("1000")
        legs = (
            TicketLeg("event-a", "winner", "a1", Decimal("2")),
            TicketLeg("event-a", "winner", "a2", Decimal("10")),
            TicketLeg("event-b", "winner", "b1", Decimal("3")),
            TicketLeg("event-b", "winner", "b2", Decimal("30")),
            TicketLeg("event-b", "winner", "b3", Decimal("300")),
        )
        tickets = [book.open_ticket([leg], "1") for leg in legs]
        group_a = {legs[0].quote_key, legs[1].quote_key}
        group_b = {legs[2].quote_key, legs[3].quote_key, legs[4].quote_key}
        engine = PortfolioEngine(max_exact_states=1, sample_count=1, seed=7)

        forward = engine.analyse(tickets, exclusive_groups=[group_a, group_b])
        reverse = engine.analyse(tickets, exclusive_groups=[group_b, group_a])

        self.assertEqual(forward, reverse)


if __name__ == "__main__":
    unittest.main()
