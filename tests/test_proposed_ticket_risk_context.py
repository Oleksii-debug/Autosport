import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

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
            "max_risk_of_ruin": Decimal("1"),
            "max_concurrent_positions": 10,
            "max_execution_slippage_fraction": Decimal("1"),
            "max_quote_age_seconds": Decimal("3600"),
        }
        values.update(overrides)
        return EconomicGoalContract(**values)  # type: ignore[arg-type]

    @classmethod
    def _context(cls, **overrides: object) -> ProposedTicketRiskContext:
        leg = cls._leg()
        values: dict[str, object] = {
            "legs": (leg,),
            "quotes": (cls._quote(leg),),
            "bankroll_id": "paper-bankroll",
            "currency": "USD",
            "proposal_ts": "2026-09-16T15:00:02+00:00",
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

    def test_policy_without_economic_goal_keeps_quote_checks_disabled(self) -> None:
        decision = self._permissive_policy().evaluate(PaperBook("100"), Decimal("1"))

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "allowed")

    def test_economic_goal_quote_controls_require_context(self) -> None:
        decision = self._permissive_policy(self._goal()).evaluate(
            PaperBook("100"), Decimal("1")
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(
            decision.reason,
            "proposed ticket risk context is required for economic goal quote checks",
        )

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
        with self.assertRaisesRegex(ValueError, "exact Decimal between 0 and 1"):
            ProposedTicketRiskContext(
                legs=(leg,),
                risk_of_ruin_upper_bound="0.01",  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(ValueError, "exact Decimal between 0 and 1"):
            ProposedTicketRiskContext(
                legs=(leg,),
                risk_of_ruin_upper_bound=Decimal("NaN"),
            )
        with self.assertRaisesRegex(ValueError, "exact Decimal between 0 and 1"):
            ProposedTicketRiskContext(
                legs=(leg,),
                risk_of_ruin_upper_bound=Decimal("1.0001"),
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

    def test_quote_age_exact_boundary_and_stale_quote_fail_closed(self) -> None:
        context = self._context(proposal_ts="2026-09-16T15:00:04+00:00")
        at_boundary = self._permissive_policy(
            self._goal(max_quote_age_seconds=Decimal("5"))
        ).evaluate(PaperBook("100"), Decimal("1"), context=context)
        self.assertTrue(at_boundary.allowed)

        stale = self._permissive_policy(
            self._goal(max_quote_age_seconds=Decimal("4.999"))
        ).evaluate(PaperBook("100"), Decimal("1"), context=context)
        self.assertFalse(stale.allowed)
        self.assertEqual(stale.reason, "quote exceeds economic goal maximum age")

    def test_missing_or_reversed_proposal_timestamp_fails_closed(self) -> None:
        missing = self._context(proposal_ts=None)
        decision = self._permissive_policy(self._goal()).evaluate(
            PaperBook("100"), Decimal("1"), context=missing
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "proposal timestamp is required for quote risk checks")

        reversed_time = self._context(proposal_ts="2026-09-16T14:59:58+00:00")
        decision = self._permissive_policy(self._goal()).evaluate(
            PaperBook("100"), Decimal("1"), context=reversed_time
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "quote timestamp is after proposal timestamp")

    def test_adverse_slippage_is_exact_bounded_and_improvement_is_not_penalized(self) -> None:
        leg = self._leg()
        quote = self._quote(leg)
        context = ProposedTicketRiskContext(
            legs=(leg,),
            quotes=(quote,),
            bankroll_id="paper-bankroll",
            currency="USD",
            proposal_ts="2026-09-16T15:00:02+00:00",
        )
        exact = (quote.decimal_odds - leg.locked_odds) / quote.decimal_odds

        allowed = self._permissive_policy(
            self._goal(max_execution_slippage_fraction=exact)
        ).evaluate(PaperBook("100"), Decimal("1"), context=context)
        self.assertTrue(allowed.allowed)

        blocked = self._permissive_policy(
            self._goal(max_execution_slippage_fraction=exact - Decimal("0.0000001"))
        ).evaluate(PaperBook("100"), Decimal("1"), context=context)
        self.assertFalse(blocked.allowed)
        self.assertEqual(
            blocked.reason,
            "quote-to-proposal slippage exceeds economic goal limit",
        )

        improved_leg = TicketLeg("event-1", "market-1", "selection-1", Decimal("2.20"))
        improved_context = ProposedTicketRiskContext(
            legs=(improved_leg,),
            quotes=(self._quote(improved_leg),),
            bankroll_id="paper-bankroll",
            currency="USD",
            proposal_ts="2026-09-16T15:00:02+00:00",
        )
        improved = self._permissive_policy(
            self._goal(max_execution_slippage_fraction=Decimal("0"))
        ).evaluate(PaperBook("100"), Decimal("1"), context=improved_context)
        self.assertTrue(improved.allowed)

    def test_positive_data_quality_floor_fails_closed_without_canonical_quality_contract(self) -> None:
        decision = self._permissive_policy(
            self._goal(minimum_data_quality=Decimal("0.1"))
        ).evaluate(PaperBook("100"), Decimal("1"), context=self._context())
        self.assertFalse(decision.allowed)
        self.assertEqual(
            decision.reason,
            "minimum data quality cannot be proven from canonical quote evidence",
        )

    def test_context_without_quote_evidence_fails_closed_for_goal_quote_checks(self) -> None:
        context = ProposedTicketRiskContext(
            legs=(self._leg(),),
            bankroll_id="paper-bankroll",
            currency="USD",
            proposal_ts="2026-09-16T15:00:02+00:00",
        )
        decision = self._permissive_policy(self._goal()).evaluate(
            PaperBook("100"), Decimal("1"), context=context
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "proposed ticket quote evidence is required")

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


    def test_event_concentration_uses_exact_whole_open_portfolio_boundary(self) -> None:
        goal = self._goal(
            max_session_loss_fraction=Decimal("1"),
            max_day_loss_fraction=Decimal("1"),
            max_drawdown_fraction=Decimal("1"),
            max_turnover_fraction=Decimal("1000"),
            max_event_concentration_fraction=Decimal("0.20"),
        )
        policy = self._permissive_policy(goal)

        at_boundary = PaperBook("100")
        at_boundary.open_ticket(
            (self._leg("event-other", "market-other", "selection-other"),),
            Decimal("80"),
        )
        allowed = policy.evaluate(
            at_boundary,
            Decimal("20"),
            context=self._context(),
        )
        self.assertTrue(allowed.allowed)

        over_limit = PaperBook("100")
        over_limit.open_ticket(
            (self._leg("event-other", "market-other", "selection-other"),),
            Decimal("79"),
        )
        over_limit.open_ticket(
            (self._leg("event-1", "market-other-2", "selection-existing"),),
            Decimal("1"),
        )
        blocked = policy.evaluate(
            over_limit,
            Decimal("20"),
            context=self._context(),
        )
        self.assertFalse(blocked.allowed)
        self.assertEqual(
            blocked.reason,
            "owner event concentration limit exceeded",
        )

    def test_market_concentration_counts_existing_open_stake_across_events(self) -> None:
        goal = self._goal(
            max_session_loss_fraction=Decimal("1"),
            max_day_loss_fraction=Decimal("1"),
            max_drawdown_fraction=Decimal("1"),
            max_turnover_fraction=Decimal("1000"),
            max_market_concentration_fraction=Decimal("0.20"),
        )
        book = PaperBook("100")
        book.open_ticket(
            (self._leg("event-other", "market-1", "selection-other"),),
            Decimal("80"),
        )

        decision = self._permissive_policy(goal).evaluate(
            book,
            Decimal("20"),
            context=self._context(),
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(
            decision.reason,
            "owner market concentration limit exceeded",
        )

    def test_provider_concentration_uses_durable_restart_provenance(self) -> None:
        goal = self._goal(
            max_session_loss_fraction=Decimal("1"),
            max_day_loss_fraction=Decimal("1"),
            max_drawdown_fraction=Decimal("1"),
            max_turnover_fraction=Decimal("1000"),
            max_provider_concentration_fraction=Decimal("0.50"),
        )
        policy = self._permissive_policy(goal)
        book = PaperBook("100")
        existing_leg = self._leg(
            "event-existing",
            "market-existing",
            "selection-existing",
        )
        book.open_ticket(
            (existing_leg,),
            Decimal("1"),
            placed_at="2026-09-16T14:00:00+00:00",
            provider_source_ids=("provider-1",),
            bankroll_id=goal.bankroll_id,
            currency=goal.currency,
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper.json"
            book.save(path)
            restarted = PaperBook.load(path)

        proposed_leg = self._leg(
            "event-provider-2",
            "market-provider-2",
            "selection-provider-2",
        )
        provider_2 = ProposedTicketRiskContext(
            legs=(proposed_leg,),
            quotes=(self._quote(proposed_leg, source_id="provider-2"),),
            bankroll_id=goal.bankroll_id,
            currency=goal.currency,
            proposal_ts="2026-09-16T15:00:02+00:00",
        )
        at_boundary = policy.evaluate(restarted, Decimal("1"), context=provider_2)
        self.assertTrue(at_boundary.allowed)

        provider_1 = ProposedTicketRiskContext(
            legs=(proposed_leg,),
            quotes=(self._quote(proposed_leg, source_id="provider-1"),),
            bankroll_id=goal.bankroll_id,
            currency=goal.currency,
            proposal_ts="2026-09-16T15:00:02+00:00",
        )
        exceeded = policy.evaluate(restarted, Decimal("1"), context=provider_1)
        self.assertFalse(exceeded.allowed)
        self.assertEqual(
            exceeded.reason,
            "owner provider concentration limit exceeded",
        )

    def test_provider_concentration_fails_closed_on_missing_historical_provenance(self) -> None:
        goal = self._goal(max_provider_concentration_fraction=Decimal("0.99"))
        book = PaperBook("100")
        book.open_ticket(
            (self._leg("legacy-event", "legacy-market", "legacy-selection"),),
            Decimal("1"),
        )

        provider = self._permissive_policy(goal).evaluate(
            book,
            Decimal("1"),
            context=self._context(),
        )
        self.assertFalse(provider.allowed)
        self.assertEqual(
            provider.reason,
            "owner provider concentration limit cannot be proven without "
            "canonical whole-portfolio exposure evidence",
        )

    def test_sport_concentration_remains_fail_closed_without_canonical_identity(self) -> None:
        sport = self._permissive_policy(
            self._goal(max_sport_concentration_fraction=Decimal("0.99"))
        ).evaluate(PaperBook("100"), Decimal("1"), context=self._context())
        self.assertFalse(sport.allowed)
        self.assertEqual(
            sport.reason,
            "owner sport concentration limit cannot be proven without "
            "canonical whole-portfolio exposure evidence",
        )


if __name__ == "__main__":
    unittest.main()
