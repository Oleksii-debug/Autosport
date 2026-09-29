import unittest
from decimal import Decimal

from autosport.candidate_search import BeamParlayCandidateSearch, CandidateLeg
from autosport.domain import TicketLeg
from autosport.paper import PaperBook
from autosport.scenario_search import PortfolioDependencyIndex, ScenarioGroup, ScenarioOutcome, ScenarioSearchEngine


class ScenarioSearchTests(unittest.TestCase):
    def test_exact_event_level_scenario_search_proves_extrema_and_expected_value(self):
        book = PaperBook("1000")
        a = TicketLeg("e1", "winner", "a", Decimal("2"))
        b = TicketLeg("e1", "winner", "b", Decimal("3"))
        ticket_a = book.open_ticket([a], "10")
        ticket_b = book.open_ticket([b], "10")
        groups = [ScenarioGroup("e1-winner", (ScenarioOutcome(a.quote_key, Decimal("0.6")), ScenarioOutcome(b.quote_key, Decimal("0.4"))))]
        report = ScenarioSearchEngine().analyse([ticket_a, ticket_b], groups)
        self.assertEqual(report.mode, "exact-enumeration")
        self.assertTrue(report.worst_proven)
        self.assertTrue(report.best_proven)
        self.assertEqual(report.total_states, 2)
        self.assertEqual(report.observed_worst, Decimal("0"))
        self.assertEqual(report.observed_best, Decimal("10"))
        self.assertEqual(report.expected_case, Decimal("4.0"))

    def test_large_space_is_truth_labeled_and_has_conservative_bounds(self):
        book = PaperBook("10000")
        tickets = []
        groups = []
        for index in range(25):
            a = TicketLeg(f"e{index}", "winner", "a", Decimal("2"))
            b = TicketLeg(f"e{index}", "winner", "b", Decimal("2"))
            tickets.append(book.open_ticket([a], "1"))
            groups.append(ScenarioGroup(f"g{index}", (ScenarioOutcome(a.quote_key), ScenarioOutcome(b.quote_key))))
        report = ScenarioSearchEngine(exact_state_limit=100, branch_node_limit=1000, sample_count=500).analyse(tickets, groups)
        self.assertEqual(report.total_states, 2 ** 25)
        self.assertIn(report.mode, {"branch-and-bound-exact-extrema", "bounded-approximation"})
        self.assertLessEqual(report.conservative_floor, report.observed_worst)
        self.assertGreaterEqual(report.conservative_ceiling, report.observed_best)

    def test_search_limits_reject_non_positive_or_non_integer_values(self):
        invalid_values = (0, -1, True, 1.0, "1")
        for field in ("exact_state_limit", "branch_node_limit", "sample_count"):
            for value in invalid_values:
                with self.subTest(field=field, value=value):
                    with self.assertRaisesRegex(ValueError, f"^{field} must be a positive non-boolean integer$"):
                        ScenarioSearchEngine(**{field: value})

    def test_search_seed_requires_non_boolean_integer(self):
        for value in (True, 1.0, "17", None):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "^seed must be a non-boolean integer$"):
                    ScenarioSearchEngine(seed=value)
        self.assertEqual(ScenarioSearchEngine(seed=0).seed, 0)
        self.assertEqual(ScenarioSearchEngine(seed=-17).seed, -17)

    def test_dependency_index_returns_only_affected_tickets(self):
        book = PaperBook("100")
        a = TicketLeg("e1", "winner", "a", Decimal("2"))
        b = TicketLeg("e2", "winner", "b", Decimal("2"))
        ta = book.open_ticket([a], "1")
        tb = book.open_ticket([b], "1")
        index = PortfolioDependencyIndex([ta, tb])
        self.assertEqual(index.affected_by({a.quote_key}), {ta.ticket_id})

    def test_beam_candidate_search_bounds_combinatorics(self):
        legs = [CandidateLeg(f"e{i}|winner|a", f"e{i}", Decimal("2.0"), Decimal("0.55")) for i in range(20)]
        results = BeamParlayCandidateSearch(beam_width=20, max_legs=5, result_limit=15).search(legs)
        self.assertLessEqual(len(results), 15)
        self.assertTrue(results)
        self.assertTrue(all(2 <= len(candidate.legs) <= 5 for candidate in results))


if __name__ == "__main__":
    unittest.main()
