import unittest
from dataclasses import replace
from decimal import Decimal, localcontext

from autosport.domain import MarketEvent, TicketLeg
from autosport.economic_goal import EconomicGoalContract
from autosport.paper import PaperBook
from autosport.risk import PaperRiskPolicy, ProposedTicketRiskContext


class EconomicGoalRiskBindingTests(unittest.TestCase):
    @staticmethod
    def _leg() -> TicketLeg:
        return TicketLeg("event-1", "market-1", "selection-1", Decimal("2"))

    @classmethod
    def _context(cls) -> ProposedTicketRiskContext:
        leg = cls._leg()
        quote = MarketEvent(
            event_id=leg.event_id,
            market_id=leg.market_id,
            selection_id=leg.selection_id,
            decimal_odds=leg.locked_odds,
            observed_ts="2026-09-16T15:00:00+00:00",
            source_id="provider-1",
            sequence=1,
            source_ts="2026-09-16T14:59:59+00:00",
            ingest_ts="2026-09-16T15:00:01+00:00",
        )
        return ProposedTicketRiskContext(
            legs=(leg,),
            quotes=(quote,),
            bankroll_id="paper-bankroll",
            currency="USD",
            proposal_ts="2026-09-16T15:00:02+00:00",
        )

    @classmethod
    def _evaluate(
        cls,
        policy: PaperRiskPolicy,
        book: PaperBook,
        stake: Decimal,
    ):
        return policy.evaluate(book, stake, context=cls._context())

    @staticmethod
    def _goal(**overrides: object) -> EconomicGoalContract:
        values: dict[str, object] = {
            "goal_id": "goal-1",
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
            "max_concurrent_positions": 10,
        }
        values.update(overrides)
        return EconomicGoalContract(**values)  # type: ignore[arg-type]

    @classmethod
    def _policy(cls, goal: EconomicGoalContract, **overrides: object) -> PaperRiskPolicy:
        values: dict[str, object] = {
            "max_ticket_fraction": Decimal("1"),
            "max_committed_fraction": Decimal("1"),
            "minimum_cash_reserve_fraction": Decimal("0"),
            "economic_goal": goal,
        }
        values.update(overrides)
        return PaperRiskPolicy(**values)  # type: ignore[arg-type]

    def test_owner_stake_fraction_tightens_executable_policy_at_exact_boundary(self) -> None:
        policy = self._policy(self._goal(max_stake_fraction=Decimal("0.10")))
        book = PaperBook("100")

        self.assertTrue(self._evaluate(policy, book, Decimal("10")).allowed)
        decision = self._evaluate(policy, book, Decimal("10.01"))

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "ticket exceeds configured bankroll fraction")

    def test_stricter_executable_ticket_fraction_cannot_be_widened_by_owner_contract(self) -> None:
        policy = self._policy(
            self._goal(max_stake_fraction=Decimal("0.50")),
            max_ticket_fraction=Decimal("0.05"),
        )
        book = PaperBook("100")

        self.assertTrue(self._evaluate(policy, book, Decimal("5")).allowed)
        self.assertFalse(self._evaluate(policy, book, Decimal("5.01")).allowed)

    def test_owner_absolute_stake_amount_is_exact_and_inclusive(self) -> None:
        policy = self._policy(self._goal(max_stake_amount=Decimal("7.50")))
        book = PaperBook("100")

        self.assertTrue(self._evaluate(policy, book, Decimal("7.50")).allowed)
        decision = self._evaluate(
            policy, book, Decimal("7.500000000000000000000000001")
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "ticket exceeds economic goal absolute stake limit")

    def test_zero_owner_absolute_stake_amount_denies_every_positive_new_stake(self) -> None:
        policy = self._policy(self._goal(max_stake_amount=Decimal("0")))
        decision = self._evaluate(
            policy, PaperBook("100"), Decimal("0.000000000000000000000000001")
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "ticket exceeds economic goal absolute stake limit")

    def test_owner_capital_at_risk_counts_existing_open_exposure_plus_proposed_stake(self) -> None:
        book = PaperBook("100")
        book.open_ticket([self._leg()], Decimal("15"))
        policy = self._policy(
            self._goal(max_capital_at_risk_fraction=Decimal("0.20"))
        )

        self.assertTrue(self._evaluate(policy, book, Decimal("5")).allowed)
        decision = self._evaluate(policy, book, Decimal("5.01"))

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "aggregate committed stake limit exceeded")

    def test_settled_historical_stake_does_not_count_as_current_capital_at_risk(self) -> None:
        book = PaperBook("100")
        settled = book.open_ticket([self._leg()], Decimal("15"))
        book.settle(settled.ticket_id, {settled.legs[0].quote_key})
        policy = self._policy(
            self._goal(max_capital_at_risk_fraction=Decimal("0.10"))
        )

        self.assertEqual(book.committed_stake, Decimal("0"))
        self.assertTrue(self._evaluate(policy, book, Decimal("10")).allowed)
        decision = self._evaluate(policy, book, Decimal("10.01"))

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "aggregate committed stake limit exceeded")


    def test_session_loss_limit_uses_conservative_realized_loss_plus_proposed_worst_case(self) -> None:
        book = PaperBook("100")
        lost = book.open_ticket([self._leg()], Decimal("4"))
        book.settle(lost.ticket_id, set())
        policy = self._policy(
            self._goal(max_session_loss_fraction=Decimal("0.05"))
        )

        self.assertTrue(self._evaluate(policy, book, Decimal("1")).allowed)
        blocked = self._evaluate(policy, book, Decimal("1.01"))

        self.assertFalse(blocked.allowed)
        self.assertEqual(
            blocked.reason,
            "economic goal conservative session loss limit exceeded",
        )

    def test_day_loss_limit_uses_same_safe_all_history_upper_bound(self) -> None:
        book = PaperBook("100")
        lost = book.open_ticket([self._leg()], Decimal("4"))
        book.settle(lost.ticket_id, set())
        policy = self._policy(
            self._goal(max_day_loss_fraction=Decimal("0.05"))
        )

        self.assertTrue(self._evaluate(policy, book, Decimal("1")).allowed)
        blocked = self._evaluate(policy, book, Decimal("1.01"))

        self.assertFalse(blocked.allowed)
        self.assertEqual(
            blocked.reason,
            "economic goal conservative day loss limit exceeded",
        )

    def test_drawdown_limit_uses_stake_basis_equity_high_watermark(self) -> None:
        book = PaperBook("100")
        winner = book.open_ticket([self._leg()], Decimal("10"))
        book.settle(winner.ticket_id, {winner.legs[0].quote_key})
        loser = book.open_ticket(
            [TicketLeg("event-2", "market-2", "selection-2", Decimal("2"))],
            Decimal("10"),
        )
        book.settle(loser.ticket_id, set())
        policy = self._policy(
            self._goal(max_drawdown_fraction=Decimal("0.10"))
        )

        self.assertTrue(self._evaluate(policy, book, Decimal("1")).allowed)
        blocked = self._evaluate(policy, book, Decimal("1.01"))

        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.reason, "economic goal drawdown limit exceeded")

    def test_turnover_limit_counts_every_durable_ticket_stake_including_settled(self) -> None:
        book = PaperBook("100")
        prior = book.open_ticket([self._leg()], Decimal("50"))
        book.settle(
            prior.ticket_id,
            set(),
            {prior.legs[0].quote_key},
        )
        policy = self._policy(
            self._goal(max_turnover_fraction=Decimal("0.51"))
        )

        self.assertTrue(self._evaluate(policy, book, Decimal("1")).allowed)
        blocked = self._evaluate(policy, book, Decimal("1.01"))

        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.reason, "economic goal turnover limit exceeded")

    def test_nontrivial_risk_of_ruin_requires_explicit_canonical_upper_bound(self) -> None:
        goal = self._goal(max_risk_of_ruin=Decimal("0.01"))
        policy = self._policy(goal)
        book = PaperBook("100")

        missing = self._evaluate(policy, book, Decimal("1"))
        self.assertFalse(missing.allowed)
        self.assertEqual(
            missing.reason,
            "portfolio risk-of-ruin evidence is required by economic goal",
        )

        at_boundary = policy.evaluate(
            book,
            Decimal("1"),
            context=replace(
                self._context(),
                risk_of_ruin_upper_bound=Decimal("0.01"),
            ),
        )
        self.assertTrue(at_boundary.allowed)

        exceeded = policy.evaluate(
            book,
            Decimal("1"),
            context=replace(
                self._context(),
                risk_of_ruin_upper_bound=Decimal("0.0100001"),
            ),
        )
        self.assertFalse(exceeded.allowed)
        self.assertEqual(
            exceeded.reason,
            "portfolio risk-of-ruin upper bound exceeds economic goal limit",
        )

    def test_owner_concurrent_position_limit_counts_only_canonical_open_tickets(self) -> None:
        book = PaperBook("100")
        ticket = book.open_ticket([self._leg()], Decimal("10"))
        policy = self._policy(self._goal(max_concurrent_positions=1))

        blocked = self._evaluate(policy, book, Decimal("1"))
        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.reason, "economic goal concurrent position limit exceeded")

        book.settle(ticket.ticket_id, {ticket.legs[0].quote_key})
        self.assertTrue(self._evaluate(policy, book, Decimal("1")).allowed)

    def test_zero_owner_concurrent_position_limit_denies_first_new_exposure(self) -> None:
        policy = self._policy(self._goal(max_concurrent_positions=0))
        decision = self._evaluate(policy, PaperBook("100"), Decimal("1"))

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "economic goal concurrent position limit exceeded")

    def test_owner_emergency_stop_denies_without_mutating_book_goal_or_decimal_context(self) -> None:
        book = PaperBook("100")
        ticket = book.open_ticket([self._leg()], Decimal("10"))
        goal = self._goal(emergency_stop=True)
        goal_before = replace(goal)
        policy = self._policy(goal)
        book_before = (
            book.balance,
            book.committed_stake,
            tuple(
                (ticket_id, value.status, value.stake)
                for ticket_id, value in book.tickets.items()
            ),
        )

        with localcontext() as caller:
            caller.prec = 3
            caller.Emax = 1
            caller.Emin = -1
            caller.clear_flags()
            decision = policy.evaluate(book, Decimal("1"))
            self.assertFalse(any(caller.flags.values()))

        book_after = (
            book.balance,
            book.committed_stake,
            tuple(
                (ticket_id, value.status, value.stake)
                for ticket_id, value in book.tickets.items()
            ),
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "economic goal emergency stop is active")
        self.assertEqual(book_after, book_before)
        self.assertEqual(goal, goal_before)
        self.assertIs(policy.economic_goal, goal)
        self.assertEqual(book.tickets[ticket.ticket_id], ticket)

    def test_non_contract_economic_goal_is_rejected_at_policy_construction(self) -> None:
        with self.assertRaisesRegex(
            TypeError, "economic_goal must be an EconomicGoalContract or None"
        ):
            PaperRiskPolicy(economic_goal=object())  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
