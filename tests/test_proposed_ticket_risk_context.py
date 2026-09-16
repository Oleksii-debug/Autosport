import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from decimal import Decimal

from autosport.economic_goal import EconomicGoalContract
from autosport.paper import PaperBook
from autosport.risk import (
    PaperRiskPolicy,
    ProposedTicketLegRiskContext,
    ProposedTicketRiskContext,
)


class ProposedTicketRiskContextTests(unittest.TestCase):
    @staticmethod
    def _leg(**overrides: object) -> ProposedTicketLegRiskContext:
        values: dict[str, object] = {
            "event_id": "event-1",
            "market_id": "market-1",
            "provider_id": "provider-1",
            "sport_id": "football",
        }
        values.update(overrides)
        return ProposedTicketLegRiskContext(**values)  # type: ignore[arg-type]

    @classmethod
    def _context(cls, **overrides: object) -> ProposedTicketRiskContext:
        values: dict[str, object] = {"legs": (cls._leg(),)}
        values.update(overrides)
        return ProposedTicketRiskContext(**values)  # type: ignore[arg-type]

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
    def _policy(cls, **goal_overrides: object) -> PaperRiskPolicy:
        return PaperRiskPolicy(
            max_ticket_fraction=Decimal("1"),
            max_committed_fraction=Decimal("1"),
            minimum_cash_reserve_fraction=Decimal("0"),
            economic_goal=cls._goal(**goal_overrides),
        )

    def test_context_is_immutable_and_preserves_only_supplied_facts(self) -> None:
        context = self._context(
            bankroll_id="paper-bankroll",
            currency="USD",
            session_id="session-1",
            day_id="2026-09-16",
            measurement_window_id="window-1",
            quote_evidence_id="quote-1",
            quote_source="provider-feed",
            quote_source_at=datetime(2026, 9, 16, 15, 0, tzinfo=timezone.utc),
            quote_observed_at=datetime(2026, 9, 16, 15, 0, 1, tzinfo=timezone.utc),
            data_quality=Decimal("0.99"),
            execution_slippage_fraction=Decimal("0.001"),
        )

        self.assertEqual(context.bankroll_id, "paper-bankroll")
        self.assertEqual(context.currency, "USD")
        self.assertEqual(context.parlay_leg_count, 1)
        self.assertEqual(context.legs[0].provider_id, "provider-1")
        self.assertEqual(context.legs[0].sport_id, "football")
        with self.assertRaises(FrozenInstanceError):
            context.bankroll_id = "other"  # type: ignore[misc]

    def test_absent_optional_facts_are_not_inferred(self) -> None:
        context = ProposedTicketRiskContext(
            legs=(
                ProposedTicketLegRiskContext(
                    event_id="event-1",
                    market_id="market-1",
                ),
            )
        )

        self.assertIsNone(context.bankroll_id)
        self.assertIsNone(context.currency)
        self.assertIsNone(context.legs[0].provider_id)
        self.assertIsNone(context.legs[0].sport_id)
        self.assertIsNone(context.quote_source)
        self.assertIsNone(context.data_quality)

    def test_parlay_leg_count_is_derived_from_exact_leg_tuple(self) -> None:
        context = ProposedTicketRiskContext(
            legs=(self._leg(), self._leg(event_id="event-2", market_id="market-2"))
        )

        self.assertEqual(context.parlay_leg_count, 2)

    def test_invalid_or_ambiguous_context_fails_at_construction(self) -> None:
        with self.assertRaisesRegex(ValueError, "legs must be a non-empty tuple"):
            ProposedTicketRiskContext(legs=())
        with self.assertRaisesRegex(ValueError, "event_id"):
            self._leg(event_id=" event-1")
        with self.assertRaisesRegex(ValueError, "currency"):
            self._context(currency="usd")
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            self._context(quote_observed_at=datetime(2026, 9, 16, 15, 0))
        with self.assertRaisesRegex(ValueError, "data_quality"):
            self._context(data_quality="0.9")
        with self.assertRaisesRegex(ValueError, "execution_slippage_fraction"):
            self._context(execution_slippage_fraction=Decimal("-0.001"))

    def test_quote_source_timestamp_cannot_follow_local_observation(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not be after"):
            self._context(
                quote_source_at=datetime(2026, 9, 16, 15, 0, 2, tzinfo=timezone.utc),
                quote_observed_at=datetime(2026, 9, 16, 15, 0, 1, tzinfo=timezone.utc),
            )

    def test_legacy_policy_call_remains_compatible(self) -> None:
        decision = self._policy().evaluate(PaperBook("100"), Decimal("1"))

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "allowed")

    def test_wrong_runtime_context_type_fails_closed(self) -> None:
        decision = self._policy().evaluate(
            PaperBook("100"),
            Decimal("1"),
            proposed_ticket_context=object(),  # type: ignore[arg-type]
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "proposed ticket risk context is invalid")

    def test_explicit_bankroll_and_currency_match_owner_contract(self) -> None:
        decision = self._policy().evaluate(
            PaperBook("100"),
            Decimal("1"),
            proposed_ticket_context=self._context(
                bankroll_id="paper-bankroll",
                currency="USD",
            ),
        )

        self.assertTrue(decision.allowed)

    def test_explicit_bankroll_mismatch_fails_closed(self) -> None:
        decision = self._policy().evaluate(
            PaperBook("100"),
            Decimal("1"),
            proposed_ticket_context=self._context(bankroll_id="other-bankroll"),
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(
            decision.reason,
            "proposed ticket bankroll_id does not match economic goal",
        )

    def test_explicit_currency_mismatch_fails_closed(self) -> None:
        decision = self._policy().evaluate(
            PaperBook("100"),
            Decimal("1"),
            proposed_ticket_context=self._context(currency="EUR"),
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(
            decision.reason,
            "proposed ticket currency does not match economic goal",
        )

    def test_context_does_not_silently_activate_unimplemented_goal_ceilings(self) -> None:
        policy = self._policy(
            blocked_providers=frozenset({"provider-1"}),
            max_parlay_legs=1,
            minimum_data_quality=Decimal("1"),
        )
        context = ProposedTicketRiskContext(
            legs=(self._leg(), self._leg(event_id="event-2", market_id="market-2")),
            data_quality=Decimal("0.5"),
        )

        decision = policy.evaluate(
            PaperBook("100"),
            Decimal("1"),
            proposed_ticket_context=context,
        )

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "allowed")


if __name__ == "__main__":
    unittest.main()
