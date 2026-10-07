import unittest
from decimal import Decimal

from autosport.candidate_optimizer import PortfolioAwareCandidateOptimizer
from autosport.candidate_search import BeamParlayCandidateSearch, CandidateLeg, ParlayCandidate
from autosport.domain import TicketLeg
from autosport.paper import PaperBook
from autosport.scenario_search import ScenarioGroup, ScenarioOutcome, ScenarioSearchEngine


def _single_candidate(leg: CandidateLeg) -> ParlayCandidate:
    expected = leg.probability * leg.decimal_odds - Decimal("1")
    return ParlayCandidate((leg,), leg.decimal_odds, leg.probability, expected)


class PortfolioAwareCandidateOptimizerTests(unittest.TestCase):
    def test_equal_standalone_ev_is_reranked_by_exact_portfolio_worst_case_change(self):
        book = PaperBook("1000")
        a_ticket_leg = TicketLeg("e1", "winner", "a", Decimal("2"))
        existing = book.open_ticket([a_ticket_leg], "10")
        a = CandidateLeg(a_ticket_leg.quote_key, "e1", Decimal("2"), Decimal("0.5"))
        b = CandidateLeg("e1|winner|b", "e1", Decimal("2"), Decimal("0.5"))
        groups = [
            ScenarioGroup(
                "e1-winner",
                (
                    ScenarioOutcome(a.quote_key, Decimal("0.5")),
                    ScenarioOutcome(b.quote_key, Decimal("0.5")),
                ),
            )
        ]

        impacts = PortfolioAwareCandidateOptimizer().evaluate_candidates(
            [existing],
            [_single_candidate(a), _single_candidate(b)],
            groups,
            stake="10",
        )

        self.assertEqual(len(impacts), 2)
        hedge, duplicate = impacts
        self.assertEqual(hedge.candidate.legs[0].quote_key, b.quote_key)
        self.assertEqual(duplicate.candidate.legs[0].quote_key, a.quote_key)
        self.assertEqual(hedge.standalone_expected_profit, Decimal("0.0"))
        self.assertEqual(duplicate.standalone_expected_profit, Decimal("0.0"))
        self.assertEqual(hedge.base_report.observed_worst, Decimal("-10"))
        self.assertEqual(hedge.with_candidate_report.observed_worst, Decimal("0"))
        self.assertEqual(hedge.observed_worst_case_change, Decimal("10"))
        self.assertEqual(duplicate.observed_worst_case_change, Decimal("-10"))
        self.assertTrue(hedge.worst_case_change_proven)
        self.assertTrue(hedge.best_case_change_proven)
        self.assertTrue(hedge.exact_marginal_extrema)
        self.assertEqual(hedge.ranking_risk_truth, "exact-worst-case-change")
        self.assertEqual(hedge.ranking_risk_change, Decimal("10"))
        self.assertEqual(hedge.expected_case_change, Decimal("0.0"))
        self.assertEqual(hedge.dependent_existing_ticket_ids, (existing.ticket_id,))
        self.assertEqual(len(book.tickets), 1)
        self.assertEqual(book.balance, Decimal("990"))

    def test_unproven_extrema_use_conservative_floor_for_ranking_not_observed_minimum(self):
        book = PaperBook("1000")
        existing_leg = TicketLeg("e1", "winner", "a", Decimal("2"))
        existing = book.open_ticket([existing_leg], "10")
        candidate_leg = CandidateLeg("e1|winner|b", "e1", Decimal("2"), Decimal("0.5"))
        groups = [
            ScenarioGroup(
                "e1-winner",
                (
                    ScenarioOutcome(existing_leg.quote_key, Decimal("0.5")),
                    ScenarioOutcome(candidate_leg.quote_key, Decimal("0.5")),
                ),
            )
        ]
        engine = ScenarioSearchEngine(exact_state_limit=1, branch_node_limit=1, sample_count=20, seed=3)
        impact = PortfolioAwareCandidateOptimizer(scenario_engine=engine).evaluate_candidates(
            [existing], [_single_candidate(candidate_leg)], groups, stake="10"
        )[0]

        self.assertFalse(impact.worst_case_change_proven)
        self.assertEqual(impact.ranking_risk_truth, "conservative-floor-change")
        self.assertEqual(impact.conservative_floor_change, Decimal("-10"))
        self.assertEqual(impact.ranking_risk_change, Decimal("-10"))

    def test_candidate_must_fit_canonical_scenario_space_and_cannot_take_two_exclusive_outcomes(self):
        a = CandidateLeg("e1|winner|a", "e1", Decimal("2"), Decimal("0.5"))
        b = CandidateLeg("e1|winner|b", "e1", Decimal("2"), Decimal("0.5"))
        missing = CandidateLeg("e2|winner|a", "e2", Decimal("2"), Decimal("0.5"))
        group = ScenarioGroup("e1", (ScenarioOutcome(a.quote_key), ScenarioOutcome(b.quote_key)))
        optimizer = PortfolioAwareCandidateOptimizer()

        impossible = ParlayCandidate((a, b), Decimal("4"), Decimal("0.25"), Decimal("0"))
        with self.assertRaisesRegex(ValueError, "mutually exclusive"):
            optimizer.evaluate_candidates([], [impossible], [group], stake="1")

        with self.assertRaisesRegex(ValueError, "missing from scenario space"):
            optimizer.evaluate_candidates([], [_single_candidate(missing)], [group], stake="1")

        malformed = CandidateLeg("not-canonical", "e1", Decimal("2"), Decimal("0.5"))
        malformed_group = ScenarioGroup("bad", (ScenarioOutcome("not-canonical"), ScenarioOutcome("other")))
        with self.assertRaisesRegex(ValueError, "not canonical"):
            optimizer.evaluate_candidates([], [_single_candidate(malformed)], [malformed_group], stake="1")

    def test_candidate_summary_fields_are_recomputed_and_forged_values_fail_closed(self):
        leg = CandidateLeg("e1|winner|a", "e1", Decimal("2"), Decimal("0.5"))
        group = ScenarioGroup("e1", (ScenarioOutcome(leg.quote_key), ScenarioOutcome("e1|winner|b")))
        optimizer = PortfolioAwareCandidateOptimizer()

        forged_odds = ParlayCandidate((leg,), Decimal("3"), Decimal("0.5"), Decimal("0.5"))
        with self.assertRaisesRegex(ValueError, "combined_odds"):
            optimizer.evaluate_candidates([], [forged_odds], [group], stake="1")

        forged_probability = ParlayCandidate((leg,), Decimal("2"), Decimal("0.6"), Decimal("0.2"))
        with self.assertRaisesRegex(ValueError, "independent_probability"):
            optimizer.evaluate_candidates([], [forged_probability], [group], stake="1")

        forged_ev = ParlayCandidate((leg,), Decimal("2"), Decimal("0.5"), Decimal("0.1"))
        with self.assertRaisesRegex(ValueError, "expected_profit_per_unit"):
            optimizer.evaluate_candidates([], [forged_ev], [group], stake="1")

        invalid_probability_leg = CandidateLeg("e1|winner|a", "e1", Decimal("2"), Decimal("1.2"))
        invalid_candidate = ParlayCandidate((invalid_probability_leg,), Decimal("2"), Decimal("1.2"), Decimal("1.4"))
        with self.assertRaisesRegex(ValueError, "probability"):
            optimizer.evaluate_candidates([], [invalid_candidate], [group], stake="1")

    def test_optimize_uses_beam_only_for_generation_then_returns_portfolio_impacts(self):
        a1 = CandidateLeg("e1|winner|a", "e1", Decimal("2"), Decimal("0.55"))
        a2 = CandidateLeg("e2|winner|a", "e2", Decimal("2"), Decimal("0.55"))
        groups = [
            ScenarioGroup(
                "e1",
                (ScenarioOutcome(a1.quote_key, Decimal("0.55")), ScenarioOutcome("e1|winner|b", Decimal("0.45"))),
            ),
            ScenarioGroup(
                "e2",
                (ScenarioOutcome(a2.quote_key, Decimal("0.55")), ScenarioOutcome("e2|winner|b", Decimal("0.45"))),
            ),
        ]
        optimizer = PortfolioAwareCandidateOptimizer(
            generator=BeamParlayCandidateSearch(beam_width=10, max_legs=2, result_limit=10),
            result_limit=5,
        )
        impacts = optimizer.optimize([], [a1, a2], groups, stake="5", minimum_legs=2)

        self.assertEqual(len(impacts), 1)
        impact = impacts[0]
        self.assertEqual(len(impact.candidate.legs), 2)
        self.assertEqual(impact.stake, Decimal("5"))
        self.assertTrue(impact.worst_case_change_proven)
        self.assertEqual(impact.ranking_risk_truth, "exact-worst-case-change")

    def test_existing_ticket_outside_supplied_scenario_space_fails_closed(self):
        book = PaperBook("100")
        existing = book.open_ticket([TicketLeg("outside", "winner", "a", Decimal("2"))], "1")
        a = CandidateLeg("e1|winner|a", "e1", Decimal("2"), Decimal("0.5"))
        b = CandidateLeg("e1|winner|b", "e1", Decimal("2"), Decimal("0.5"))
        group = ScenarioGroup("e1", (ScenarioOutcome(a.quote_key), ScenarioOutcome(b.quote_key)))
        with self.assertRaisesRegex(ValueError, "missing from scenario space"):
            PortfolioAwareCandidateOptimizer().evaluate_candidates([existing], [_single_candidate(a)], [group], stake="1")



    def test_optimizer_rejects_input_list_subclasses_before_container_dispatch(self):
        class HostileList(list):
            def __iter__(self):
                raise AssertionError("list subclass iteration must not execute")

            def __bool__(self):
                raise AssertionError("list subclass truthiness must not execute")

        leg = CandidateLeg("e1|winner|a", "e1", Decimal("2"), Decimal("0.5"))
        candidate = _single_candidate(leg)
        group = ScenarioGroup(
            "e1",
            (ScenarioOutcome(leg.quote_key), ScenarioOutcome("e1|winner|b")),
        )
        optimizer = PortfolioAwareCandidateOptimizer()

        with self.assertRaisesRegex(ValueError, "existing_tickets must be an exact list"):
            optimizer.evaluate_candidates(
                HostileList(), [candidate], [group], stake="1"  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(ValueError, "candidates must be an exact list"):
            optimizer.evaluate_candidates(
                [], HostileList([candidate]), [group], stake="1"  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(ValueError, "groups must be an exact list"):
            optimizer.evaluate_candidates(
                [], [candidate], HostileList([group]), stake="1"  # type: ignore[arg-type]
            )

    def test_optimizer_rejects_existing_ticket_subclass_before_status_dispatch(self):
        class HostileTicket(type(PaperBook("100").open_ticket(
            [TicketLeg("fixture", "winner", "a", Decimal("2"))], "1"
        ))):
            def __getattribute__(self, name):
                if name == "status":
                    raise AssertionError("ticket subclass status dispatch must not execute")
                return super().__getattribute__(name)

        base = PaperBook("100").open_ticket(
            [TicketLeg("e0", "winner", "a", Decimal("2"))], "1"
        )
        hostile = HostileTicket(
            ticket_id=base.ticket_id,
            stake=base.stake,
            legs=base.legs,
            placed_at=base.placed_at,
            status=base.status,
            payout=base.payout,
            strategy_reason=base.strategy_reason,
            provider_source_ids=base.provider_source_ids,
            provider_accounts=base.provider_accounts,
            bankroll_id=base.bankroll_id,
            currency=base.currency,
            settled_at=base.settled_at,
        )
        leg = CandidateLeg("e1|winner|a", "e1", Decimal("2"), Decimal("0.5"))
        group = ScenarioGroup(
            "e1",
            (ScenarioOutcome(leg.quote_key), ScenarioOutcome("e1|winner|b")),
        )

        with self.assertRaisesRegex(
            ValueError,
            "existing_tickets must contain exact PaperTicket values",
        ):
            PortfolioAwareCandidateOptimizer().evaluate_candidates(
                [hostile], [_single_candidate(leg)], [group], stake="1"
            )

    def test_optimizer_rejects_parlay_candidate_subclass_before_member_dispatch(self):
        class HostileCandidate(ParlayCandidate):
            def __getattribute__(self, name):
                if name == "legs":
                    raise AssertionError("candidate subclass member dispatch must not execute")
                return super().__getattribute__(name)

        leg = CandidateLeg("e1|winner|a", "e1", Decimal("2"), Decimal("0.5"))
        candidate = HostileCandidate((leg,), Decimal("2"), Decimal("0.5"), Decimal("0"))
        group = ScenarioGroup(
            "e1",
            (ScenarioOutcome(leg.quote_key), ScenarioOutcome("e1|winner|b")),
        )

        with self.assertRaisesRegex(ValueError, "candidate must be an exact ParlayCandidate"):
            PortfolioAwareCandidateOptimizer().evaluate_candidates(
                [], [candidate], [group], stake="1"
            )

    def test_optimizer_rejects_candidate_leg_tuple_subclass_before_iteration(self):
        class HostileLegTuple(tuple):
            def __iter__(self):
                raise AssertionError("candidate tuple iteration must not execute")

            def __bool__(self):
                raise AssertionError("candidate tuple truthiness must not execute")

        leg = CandidateLeg("e1|winner|a", "e1", Decimal("2"), Decimal("0.5"))
        candidate = ParlayCandidate(
            HostileLegTuple((leg,)),  # type: ignore[arg-type]
            Decimal("2"),
            Decimal("0.5"),
            Decimal("0"),
        )
        group = ScenarioGroup(
            "e1",
            (ScenarioOutcome(leg.quote_key), ScenarioOutcome("e1|winner|b")),
        )

        with self.assertRaisesRegex(ValueError, "candidate legs must be an exact tuple"):
            PortfolioAwareCandidateOptimizer().evaluate_candidates(
                [], [candidate], [group], stake="1"
            )

    def test_optimizer_rejects_candidate_leg_subclass_before_identity_dispatch(self):
        class HostileLeg(CandidateLeg):
            def ticket_identity(self):
                raise AssertionError("candidate leg subclass identity dispatch must not execute")

        leg = HostileLeg(
            "e1|winner|a",
            "e1",
            Decimal("2"),
            Decimal("0.5"),
        )
        candidate = ParlayCandidate((leg,), Decimal("2"), Decimal("0.5"), Decimal("0"))
        group = ScenarioGroup(
            "e1",
            (ScenarioOutcome(leg.quote_key), ScenarioOutcome("e1|winner|b")),
        )

        with self.assertRaisesRegex(
            ValueError,
            "candidate legs must contain exact CandidateLeg values",
        ):
            PortfolioAwareCandidateOptimizer().evaluate_candidates(
                [], [candidate], [group], stake="1"
            )

    def test_optimizer_revalidates_mutated_candidate_identity_before_sort_or_hash(self):
        class HostileQuoteKey(str):
            def __hash__(self):
                raise AssertionError("candidate quote hash must not execute before admission")

            def strip(self, *args, **kwargs):
                raise AssertionError("candidate quote strip must not dispatch on subclass")

        leg = CandidateLeg("e1|winner|a", "e1", Decimal("2"), Decimal("0.5"))
        object.__setattr__(leg, "quote_key", HostileQuoteKey(leg.quote_key))
        candidate = ParlayCandidate((leg,), Decimal("2"), Decimal("0.5"), Decimal("0"))
        group = ScenarioGroup(
            "e1",
            (
                ScenarioOutcome("e1|winner|a"),
                ScenarioOutcome("e1|winner|b"),
            ),
        )

        with self.assertRaisesRegex(ValueError, "candidate leg identity is invalid"):
            PortfolioAwareCandidateOptimizer().evaluate_candidates(
                [], [candidate], [group], stake="1"
            )

    def test_optimizer_revalidates_mutated_scenario_identity_before_group_mapping(self):
        class HostileQuoteKey(str):
            def __hash__(self):
                raise AssertionError("scenario quote hash dispatched before exact admission")

        a = CandidateLeg("e1|winner|a", "e1", Decimal("2"), Decimal("0.5"))
        b = CandidateLeg("e1|winner|b", "e1", Decimal("2"), Decimal("0.5"))
        first = ScenarioOutcome(a.quote_key, Decimal("0.5"))
        group = ScenarioGroup(
            "e1-winner",
            (
                first,
                ScenarioOutcome(b.quote_key, Decimal("0.5")),
            ),
        )
        object.__setattr__(first, "quote_key", HostileQuoteKey(first.quote_key))

        with self.assertRaisesRegex(
            ValueError,
            "scenario outcome quote_key must be a non-empty trimmed string",
        ):
            PortfolioAwareCandidateOptimizer().evaluate_candidates(
                [],
                [_single_candidate(a)],
                [group],
                stake="10",
            )


if __name__ == "__main__":
    unittest.main()
