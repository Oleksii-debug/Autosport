import unittest
from decimal import Decimal

from autosport.domain import MarketEvent, TicketLeg
from autosport.economic_goal import EconomicGoalContract
from autosport.paper import PaperBook
from autosport.risk import PaperRiskPolicy, ProposedTicketRiskContext


class ProposedTicketRiskContextTests(unittest.TestCase):
    @staticmethod
    def _leg(
        event_id: str = "event-1",
        market_id: str = "market-1",
        selection_id: str = "selection-1",
    ) -> TicketLeg:
        return TicketLeg(event_id, market_id, selection_id, Decimal("2"))

    @staticmethod
    def _quote(
        leg: TicketLeg,
        *,
        source_id: str = "provider-1",
        sequence: int = 1,
        observed_ts: str = "2026-09-16T15:00:00+00:00",
        source_ts: str | None = "2026-09-16T14:59:59+00:00",
    ) -> MarketEvent:
        return MarketEvent(
            event_id=leg.event_id,
            market_id=leg.market_id,
            selection_id=leg.selection_id,
            decimal_odds=Decimal("2.10"),
            observed_ts=observed_ts,
            source_id=source_id,
            sequence=sequence,
            source_ts=source_ts,
            ingest_ts="2026-09-16T15:00:01+00:00",
        )

    @staticmethod
    def _goal(**overrides: object) -> EconomicGoalContract:
        values: dict[str, object] = {
            "goal_id": "goal-1",
            "revision": 1,
            "bankroll_id": "paper-bankroll",
            "currency": "USD",
            "max_stake_fraction": Decimal("1"),
            "max_capital_at_risk_fraction": Decimal("1"),
            "max_concurrent_positions": 10,
        }
        values.update(overrides)
        return EconomicGoalContract(**values)  # type: ignore[arg-type]

    @classmethod
    def _context(cls, **overrides: object) -> ProposedTicketRiskContext:
        leg = cls._leg()
        values: dict[str, object] = {
            "legs": (leg,),
            "quotes": (cls._quote(leg),),
        }
        values.update(overrides)
        return ProposedTicketRiskContext(**values)  # type: ignore[arg-type]

    @staticmethod
    def _permissive_policy(
        goal: EconomicGoalContract | None = None,
    ) -> PaperRiskPolicy:
        return PaperRiskPolicy(
            max_ticket_fraction=Decimal("1"),
            max_committed_fraction=Decimal("1"),
            minimum_cash_reserve_fraction=Decimal("0"),
            economic_goal=goal,
        )

    def test_context_preserves_canonical_proposal_and_quote_identity(self) -> None:
        leg = self._leg("event-A", "market-B", "selection-C")
        quote = self._quote(leg, source_id="provider-X")
        context = ProposedTicketRiskContext(
            legs=(leg,),
            quotes=(quote,),
            bankroll_id="paper-bankroll",
            currency="USD",
            measurement_window_start="2026-09-16T14:00:00+00:00",
            measurement_window_end="2026-09-16T16:00:00+00:00",
        )

        self.assertIs(context.legs[0], leg)
        self.assertIs(context.quotes[0], quote)
        self.assertEqual(context.event_ids, frozenset({"event-A"}))
        self.assertEqual(context.market_ids, frozenset({"market-B"}))
        self.assertEqual(context.source_ids, frozenset({"provider-X"}))
        self.assertEqual(context.parlay_leg_count, 1)
        self.assertEqual(context.bankroll_id, "paper-bankroll")
        self.assertEqual(context.currency, "USD")

    def test_legacy_policy_call_remains_compatible(self) -> None:
        decision = self._permissive_policy().evaluate(PaperBook("100"), Decimal("1"))

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "allowed")

    def test_policy_accepts_valid_context_without_creating_new_state_authority(self) -> None:
        book = PaperBook("100")
        context = self._context(
            bankroll_id="paper-bankroll",
            currency="USD",
        )
        decision = self._permissive_policy(self._goal()).evaluate(
            book,
            Decimal("1"),
            context=context,
        )

        self.assertTrue(decision.allowed)
        self.assertEqual(book.balance, Decimal("100"))
        self.assertEqual(book.tickets, {})

    def test_context_rejects_missing_or_duplicate_proposal_identity(self) -> None:
        with self.assertRaisesRegex(ValueError, "non-empty tuple of legs"):
            ProposedTicketRiskContext(legs=())

        duplicate = self._leg()
        with self.assertRaisesRegex(ValueError, "duplicate leg identity"):
            ProposedTicketRiskContext(legs=(duplicate, duplicate))

        with self.assertRaisesRegex(ValueError, "invalid leg"):
            ProposedTicketRiskContext(
                legs=(TicketLeg(" event-1", "market-1", "selection-1", Decimal("2")),)
            )

    def test_quote_evidence_must_be_canonical_and_cover_all_legs(self) -> None:
        first = self._leg("event-1", "market-1", "selection-1")
        second = self._leg("event-2", "market-2", "selection-2")

        with self.assertRaisesRegex(ValueError, "cover every proposed leg exactly once"):
            ProposedTicketRiskContext(
                legs=(first, second),
                quotes=(self._quote(first),),
            )

        with self.assertRaisesRegex(ValueError, "invalid quote"):
            ProposedTicketRiskContext(
                legs=(first,),
                quotes=(
                    self._quote(
                        first,
                        observed_ts="2026-09-16T15:00:00",
                    ),
                ),
            )

    def test_optional_bankroll_currency_and_measurement_window_fail_closed(self) -> None:
        leg = self._leg()

        with self.assertRaisesRegex(ValueError, "bankroll_id and currency"):
            ProposedTicketRiskContext(legs=(leg,), bankroll_id="paper-bankroll")
        with self.assertRaisesRegex(ValueError, "uppercase ASCII"):
            ProposedTicketRiskContext(
                legs=(leg,),
                bankroll_id="paper-bankroll",
                currency="usd",
            )
        with self.assertRaisesRegex(ValueError, "start and end"):
            ProposedTicketRiskContext(
                legs=(leg,),
                measurement_window_start="2026-09-16T15:00:00+00:00",
            )
        with self.assertRaisesRegex(ValueError, "start must not be after end"):
            ProposedTicketRiskContext(
                legs=(leg,),
                measurement_window_start="2026-09-16T16:00:00+00:00",
                measurement_window_end="2026-09-16T15:00:00+00:00",
            )

    def test_policy_rejects_untyped_context_and_goal_identity_mismatch(self) -> None:
        book = PaperBook("100")
        policy = self._permissive_policy(self._goal())

        wrong_type = policy.evaluate(
            book,
            Decimal("1"),
            context=object(),  # type: ignore[arg-type]
        )
        self.assertFalse(wrong_type.allowed)
        self.assertEqual(wrong_type.reason, "proposed ticket risk context is invalid")

        wrong_bankroll = policy.evaluate(
            book,
            Decimal("1"),
            context=self._context(bankroll_id="other-bankroll", currency="USD"),
        )
        self.assertFalse(wrong_bankroll.allowed)
        self.assertEqual(
            wrong_bankroll.reason,
            "proposed ticket bankroll identity does not match economic goal",
        )

        wrong_currency = policy.evaluate(
            book,
            Decimal("1"),
            context=self._context(bankroll_id="paper-bankroll", currency="EUR"),
        )
        self.assertFalse(wrong_currency.allowed)
        self.assertEqual(
            wrong_currency.reason,
            "proposed ticket currency does not match economic goal",
        )

    def test_existing_economic_goal_limit_remains_authoritative_with_context(self) -> None:
        policy = self._permissive_policy(
            self._goal(max_stake_amount=Decimal("1"))
        )
        context = self._context(
            bankroll_id="paper-bankroll",
            currency="USD",
        )

        decision = policy.evaluate(PaperBook("100"), Decimal("1.01"), context=context)

        self.assertFalse(decision.allowed)
        self.assertEqual(
            decision.reason,
            "ticket exceeds economic goal absolute stake limit",
        )


if __name__ == "__main__":
    unittest.main()
