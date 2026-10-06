import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.domain import MarketEvent, TicketLeg
from autosport.economic_goal import EconomicGoalContract
from autosport.paper import PaperBook
from autosport.risk import PaperRiskPolicy, ProposedTicketRiskContext


class CorrelatedExposureAdversarialTests(unittest.TestCase):
    @staticmethod
    def _goal(**overrides: object) -> EconomicGoalContract:
        values: dict[str, object] = {
            "goal_id": "goal-correlated-exposure",
            "revision": 1,
            "bankroll_id": "paper-bankroll",
            "currency": "USD",
            "max_stake_fraction": Decimal("1"),
            "max_session_loss_fraction": Decimal("1"),
            "max_day_loss_fraction": Decimal("1"),
            "max_drawdown_fraction": Decimal("1"),
            "max_capital_at_risk_fraction": Decimal("1"),
            "max_turnover_fraction": Decimal("1000"),
            "max_risk_of_ruin": Decimal("1"),
            "max_concurrent_positions": 20,
            "max_execution_slippage_fraction": Decimal("1"),
            "max_quote_age_seconds": Decimal("3600"),
        }
        values.update(overrides)
        return EconomicGoalContract(**values)  # type: ignore[arg-type]

    @staticmethod
    def _policy(goal: EconomicGoalContract) -> PaperRiskPolicy:
        return PaperRiskPolicy(
            max_ticket_fraction=Decimal("1"),
            max_committed_fraction=Decimal("1"),
            minimum_cash_reserve_fraction=Decimal("0"),
            economic_goal=goal,
        )

    @staticmethod
    def _leg(event_id: str, market_id: str, selection_id: str) -> TicketLeg:
        return TicketLeg(event_id, market_id, selection_id, Decimal("2"))

    @classmethod
    def _context(
        cls,
        event_id: str,
        market_id: str,
        selection_id: str,
        *,
        source_id: str = "provider-1",
        account_id: str | None = None,
        sequence: int = 1,
    ) -> ProposedTicketRiskContext:
        leg = cls._leg(event_id, market_id, selection_id)
        quote = MarketEvent(
            event_id=event_id,
            market_id=market_id,
            selection_id=selection_id,
            decimal_odds=Decimal("2.10"),
            observed_ts="2026-09-16T15:00:00+00:00",
            source_id=source_id,
            sequence=sequence,
            source_ts="2026-09-16T14:59:59+00:00",
            ingest_ts="2026-09-16T15:00:01+00:00",
        )
        provider_accounts = (
            ((source_id, account_id),) if account_id is not None else ()
        )
        return ProposedTicketRiskContext(
            legs=(leg,),
            quotes=(quote,),
            provider_accounts=provider_accounts,
            bankroll_id="paper-bankroll",
            currency="USD",
            proposal_ts="2026-09-16T15:00:02+00:00",
        )

    @staticmethod
    def _by_identity(
        contexts: tuple[ProposedTicketRiskContext, ...],
        stakes: tuple[Decimal, ...],
    ) -> dict[str, Decimal]:
        return {
            context.legs[0].quote_key: stake
            for context, stake in zip(contexts, stakes, strict=True)
        }

    def test_single_candidate_without_correlation_metadata_preserves_existing_allocation(
        self,
    ) -> None:
        context = self._context("event-a", "market-a", "selection-a")
        decision = self._policy(self._goal()).derive_goal_stake_vector(
            PaperBook("100"),
            (Decimal("0.25"),),
            contexts=(context,),
        )

        self.assertEqual(decision.action, "STAKE_VECTOR")
        self.assertEqual(decision.stakes, (Decimal("25"),))

    def test_duplicate_semantic_candidate_fails_closed_without_amplifying_risk(
        self,
    ) -> None:
        first = self._context("event-a", "market-a", "selection-a")
        duplicate = self._context("event-a", "market-a", "selection-a")
        self.assertIsNot(first, duplicate)

        decision = self._policy(self._goal()).derive_goal_stake_vector(
            PaperBook("100"),
            (Decimal("0.30"), Decimal("0.20")),
            contexts=(first, duplicate),
        )

        self.assertEqual(decision.action, "WAIT")
        self.assertEqual(decision.stakes, (Decimal("0"), Decimal("0")))
        self.assertEqual(
            decision.reason,
            "candidate set contains duplicate or ambiguous executable identity",
        )

    def test_disjoint_candidate_permutation_is_invariant_under_capital_scarcity(
        self,
    ) -> None:
        first = self._context(
            "event-a", "market-a", "selection-a", source_id="provider-a", sequence=1
        )
        second = self._context(
            "event-b", "market-b", "selection-b", source_id="provider-b", sequence=2
        )
        policy = self._policy(
            self._goal(max_capital_at_risk_fraction=Decimal("0.30"))
        )
        book = PaperBook("100")
        signals = (Decimal("0.25"), Decimal("0.25"))

        forward = policy.derive_goal_stake_vector(
            book,
            signals,
            contexts=(first, second),
        )
        reverse = policy.derive_goal_stake_vector(
            book,
            signals,
            contexts=(second, first),
        )

        self.assertEqual(forward.action, "STAKE_VECTOR")
        self.assertEqual(reverse.action, "STAKE_VECTOR")
        forward_by_identity = self._by_identity((first, second), forward.stakes)
        reverse_by_identity = self._by_identity((second, first), reverse.stakes)
        self.assertEqual(forward_by_identity, reverse_by_identity)
        self.assertEqual(
            forward_by_identity,
            {
                first.legs[0].quote_key: Decimal("25"),
                second.legs[0].quote_key: Decimal("5"),
            },
        )
        self.assertEqual(book.balance, Decimal("100"))
        self.assertEqual(book.tickets, {})

    def test_same_event_candidates_are_sequentially_concentration_bounded(self) -> None:
        goal = self._goal(max_event_concentration_fraction=Decimal("0.15"))
        policy = self._policy(goal)
        book = PaperBook("100")
        book.open_ticket(
            (self._leg("event-existing", "market-existing", "selection-existing"),),
            Decimal("80"),
            placed_at="2026-09-16T14:00:00+00:00",
        )
        first = self._context("event-risk", "market-a", "selection-a", sequence=1)
        second = self._context("event-risk", "market-b", "selection-b", sequence=2)

        decision = policy.derive_goal_stake_vector(
            book,
            (Decimal("0.10"), Decimal("0.10")),
            contexts=(first, second),
        )

        self.assertEqual(decision.action, "STAKE_VECTOR")
        self.assertEqual(decision.stakes, (Decimal("10"), Decimal("0")))
        self.assertEqual(book.balance, Decimal("20"))
        self.assertEqual(len(book.tickets), 1)

    def test_distinct_selections_in_one_market_receive_no_unproven_hedge_credit(self) -> None:
        goal = self._goal(max_market_concentration_fraction=Decimal("0.15"))
        policy = self._policy(goal)
        book = PaperBook("100")
        book.open_ticket(
            (self._leg("event-existing", "market-existing", "selection-existing"),),
            Decimal("80"),
            placed_at="2026-09-16T14:00:00+00:00",
        )
        first = self._context("event-risk", "market-risk", "selection-a", sequence=1)
        second = self._context("event-risk", "market-risk", "selection-b", sequence=2)

        decision = policy.derive_goal_stake_vector(
            book,
            (Decimal("0.10"), Decimal("0.10")),
            contexts=(first, second),
        )

        self.assertEqual(decision.action, "STAKE_VECTOR")
        self.assertEqual(decision.stakes, (Decimal("10"), Decimal("0")))

    def test_same_provider_account_candidates_are_common_mode_concentration(self) -> None:
        goal = self._goal(max_provider_concentration_fraction=Decimal("0.15"))
        policy = self._policy(goal)
        book = PaperBook("100")
        book.open_ticket(
            (self._leg("event-existing", "market-existing", "selection-existing"),),
            Decimal("80"),
            placed_at="2026-09-16T14:00:00+00:00",
            provider_source_ids=("provider-0",),
            provider_accounts=(("provider-0", "account-0"),),
            bankroll_id=goal.bankroll_id,
            currency=goal.currency,
        )
        first = self._context(
            "event-a",
            "market-a",
            "selection-a",
            source_id="provider-risk",
            account_id="account-risk",
            sequence=1,
        )
        second = self._context(
            "event-b",
            "market-b",
            "selection-b",
            source_id="provider-risk",
            account_id="account-risk",
            sequence=2,
        )

        decision = policy.derive_goal_stake_vector(
            book,
            (Decimal("0.10"), Decimal("0.10")),
            contexts=(first, second),
        )

        self.assertEqual(decision.action, "STAKE_VECTOR")
        self.assertEqual(decision.stakes, (Decimal("10"), Decimal("0")))

    def test_restart_replays_the_same_identity_bound_vector(self) -> None:
        goal = self._goal()
        policy = self._policy(goal)
        book = PaperBook("100")
        book.open_ticket(
            (self._leg("event-existing", "market-existing", "selection-existing"),),
            Decimal("20"),
            placed_at="2026-09-16T14:00:00+00:00",
        )
        contexts = (
            self._context(
                "event-a", "market-a", "selection-a", source_id="provider-a", sequence=1
            ),
            self._context(
                "event-b", "market-b", "selection-b", source_id="provider-b", sequence=2
            ),
        )
        signals = (Decimal("0.25"), Decimal("0.15"))

        before = policy.derive_goal_stake_vector(book, signals, contexts=contexts)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper.json"
            book.save(path)
            restarted = PaperBook.load(path)
        after = policy.derive_goal_stake_vector(restarted, signals, contexts=contexts)

        self.assertEqual(after, before)
        self.assertEqual(restarted.balance, book.balance)
        self.assertEqual(set(restarted.tickets), set(book.tickets))


if __name__ == "__main__":
    unittest.main()
