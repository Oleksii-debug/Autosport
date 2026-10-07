import unittest
from decimal import Decimal

from autosport.candidate_search import BeamParlayCandidateSearch, CandidateLeg
from autosport.domain import TicketLeg
from autosport.paper import PaperBook
from autosport.scenario_search import PortfolioDependencyIndex, ScenarioGroup, ScenarioOutcome, ScenarioSearchEngine


class _ExplosiveQuoteKey(str):
    def __hash__(self) -> int:
        raise AssertionError("hostile quote_key hash dispatched before exact admission")


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

    def test_scenario_outcome_rejects_hostile_quote_key_before_hash_dispatch(self):
        with self.assertRaisesRegex(
            ValueError,
            "scenario outcome quote_key must be a non-empty trimmed string",
        ):
            ScenarioOutcome(_ExplosiveQuoteKey("e1|winner|a"))

    def test_scenario_group_revalidates_mutated_outcome_before_duplicate_hashing(self):
        hostile = ScenarioOutcome("e1|winner|a")
        object.__setattr__(
            hostile,
            "quote_key",
            _ExplosiveQuoteKey("e1|winner|a"),
        )

        with self.assertRaisesRegex(
            ValueError,
            "scenario outcome quote_key must be a non-empty trimmed string",
        ):
            ScenarioGroup(
                "e1-winner",
                (
                    hostile,
                    ScenarioOutcome("e1|winner|b"),
                ),
            )

    def test_scenario_search_rejects_ticket_list_subclass_before_iteration(self):
        class HostileTicketList(list):
            def __iter__(self):
                raise AssertionError(
                    "scenario ticket list iterated before exact-list admission"
                )

            def __len__(self):
                raise AssertionError(
                    "scenario ticket list length dispatched before exact-list admission"
                )

        with self.assertRaisesRegex(
            ValueError,
            "portfolio tickets must be an exact list",
        ):
            ScenarioSearchEngine().analyse(
                HostileTicketList(),  # type: ignore[arg-type]
                [],
            )

    def test_scenario_search_revalidates_mutated_group_identity_at_use_boundary(self):
        book = PaperBook("100")
        a = TicketLeg("e1", "winner", "a", Decimal("2"))
        b = TicketLeg("e1", "winner", "b", Decimal("2"))
        ticket = book.open_ticket([a], "10")
        first = ScenarioOutcome(a.quote_key)
        group = ScenarioGroup(
            "e1-winner",
            (
                first,
                ScenarioOutcome(b.quote_key),
            ),
        )
        object.__setattr__(
            first,
            "quote_key",
            _ExplosiveQuoteKey(a.quote_key),
        )

        with self.assertRaisesRegex(
            ValueError,
            "scenario outcome quote_key must be a non-empty trimmed string",
        ):
            ScenarioSearchEngine().analyse([ticket], [group])

    def test_dependency_index_returns_only_affected_tickets(self):
        book = PaperBook("100")
        a = TicketLeg("e1", "winner", "a", Decimal("2"))
        b = TicketLeg("e2", "winner", "b", Decimal("2"))
        ta = book.open_ticket([a], "1")
        tb = book.open_ticket([b], "1")
        index = PortfolioDependencyIndex([ta, tb])
        self.assertEqual(index.affected_by({a.quote_key}), {ta.ticket_id})

    def test_dependency_index_rejects_ticket_list_subclass_before_iteration(self):
        class HostileTicketList(list):
            def __iter__(self):
                raise AssertionError(
                    "dependency ticket list iterated before exact-list admission"
                )

        with self.assertRaisesRegex(
            ValueError,
            "portfolio dependency tickets must be an exact list",
        ):
            PortfolioDependencyIndex(HostileTicketList())  # type: ignore[arg-type]

    def test_dependency_index_rejects_quote_set_subclass_before_iteration(self):
        class HostileQuoteSet(set):
            def __iter__(self):
                raise AssertionError(
                    "dependency quote set iterated before exact-set admission"
                )

        index = PortfolioDependencyIndex([])
        with self.assertRaisesRegex(
            ValueError,
            "portfolio dependency quote_keys must be an exact set",
        ):
            index.affected_by(HostileQuoteSet({"e1|winner|a"}))  # type: ignore[arg-type]

    def test_dependency_index_rejects_hostile_ticket_id_before_hash_dispatch(self):
        book = PaperBook("100")
        leg = TicketLeg("e1", "winner", "a", Decimal("2"))
        ticket = book.open_ticket([leg], "1")
        ticket.ticket_id = _ExplosiveQuoteKey(ticket.ticket_id)

        with self.assertRaisesRegex(
            ValueError,
            "portfolio dependency ticket_id must be a non-empty trimmed string",
        ):
            PortfolioDependencyIndex([ticket])

    def test_dependency_index_revalidates_mutated_leg_identity_before_publish(self):
        book = PaperBook("100")
        leg = TicketLeg("e1", "winner", "a", Decimal("2"))
        ticket = book.open_ticket([leg], "1")
        object.__setattr__(
            leg,
            "event_id",
            _ExplosiveQuoteKey("e1"),
        )

        with self.assertRaisesRegex(
            ValueError,
            "event_id must be a non-empty trimmed string",
        ):
            PortfolioDependencyIndex([ticket])

    def test_beam_candidate_search_bounds_combinatorics(self):
        legs = [CandidateLeg(f"e{i}|winner|a", f"e{i}", Decimal("2.0"), Decimal("0.55")) for i in range(20)]
        results = BeamParlayCandidateSearch(beam_width=20, max_legs=5, result_limit=15).search(legs)
        self.assertLessEqual(len(results), 15)
        self.assertTrue(results)
        self.assertTrue(all(2 <= len(candidate.legs) <= 5 for candidate in results))


if __name__ == "__main__":
    unittest.main()
