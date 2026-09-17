import unittest
from decimal import Decimal

from autosport.domain import MarketEvent, TicketLeg
from autosport.economic_goal import EconomicGoalContract
from autosport.paper import PaperBook
from autosport.risk import PaperRiskPolicy, ProposedTicketRiskContext


class EconomicGoalProposalRestrictionTests(unittest.TestCase):
    @staticmethod
    def _goal(**overrides: object) -> EconomicGoalContract:
        values: dict[str, object] = {
            "goal_id": "goal-restrictions",
            "revision": 1,
            "bankroll_id": "paper-bankroll",
            "currency": "USD",
            "max_stake_fraction": Decimal("1"),
            "max_capital_at_risk_fraction": Decimal("1"),
            "max_concurrent_positions": 10,
            "max_parlay_legs": 10,
        }
        values.update(overrides)
        return EconomicGoalContract(**values)  # type: ignore[arg-type]

    @staticmethod
    def _context(
        *,
        market_ids: tuple[str, ...] = ("market-1",),
        source_ids: tuple[str, ...] = ("provider-1",),
    ) -> ProposedTicketRiskContext:
        if len(market_ids) != len(source_ids):
            raise ValueError("test market/source fixtures must have equal length")
        legs: list[TicketLeg] = []
        quotes: list[MarketEvent] = []
        for index, (market_id, source_id) in enumerate(
            zip(market_ids, source_ids, strict=True), start=1
        ):
            leg = TicketLeg(
                f"event-{index}",
                market_id,
                f"selection-{index}",
                Decimal("2"),
            )
            legs.append(leg)
            quotes.append(
                MarketEvent(
                    event_id=leg.event_id,
                    market_id=leg.market_id,
                    selection_id=leg.selection_id,
                    decimal_odds=leg.locked_odds,
                    observed_ts="2026-09-17T14:00:00+00:00",
                    source_id=source_id,
                    sequence=index,
                    source_ts="2026-09-17T14:00:00+00:00",
                    ingest_ts="2026-09-17T14:00:01+00:00",
                )
            )
        return ProposedTicketRiskContext(
            legs=tuple(legs),
            quotes=tuple(quotes),
            proposal_ts="2026-09-17T14:00:02+00:00",
        )

    @staticmethod
    def _policy(goal: EconomicGoalContract) -> PaperRiskPolicy:
        return PaperRiskPolicy(
            max_ticket_fraction=Decimal("1"),
            max_committed_fraction=Decimal("1"),
            minimum_cash_reserve_fraction=Decimal("0"),
            economic_goal=goal,
        )

    def test_owner_parlay_limit_is_inclusive_and_blocks_excess_legs(self) -> None:
        policy = self._policy(self._goal(max_parlay_legs=1))

        single = policy.evaluate(
            PaperBook("100"),
            Decimal("1"),
            context=self._context(),
        )
        parlay = policy.evaluate(
            PaperBook("100"),
            Decimal("1"),
            context=self._context(
                market_ids=("market-1", "market-2"),
                source_ids=("provider-1", "provider-2"),
            ),
        )

        self.assertTrue(single.allowed)
        self.assertFalse(parlay.allowed)
        self.assertEqual(parlay.reason, "economic goal parlay leg limit exceeded")

    def test_owner_blocked_market_denies_matching_proposal(self) -> None:
        decision = self._policy(
            self._goal(blocked_markets=frozenset({"market-1"}))
        ).evaluate(PaperBook("100"), Decimal("1"), context=self._context())

        self.assertFalse(decision.allowed)
        self.assertEqual(
            decision.reason,
            "proposed ticket contains an owner-blocked market",
        )

    def test_owner_blocked_provider_denies_matching_quote_source(self) -> None:
        decision = self._policy(
            self._goal(blocked_providers=frozenset({"provider-1"}))
        ).evaluate(PaperBook("100"), Decimal("1"), context=self._context())

        self.assertFalse(decision.allowed)
        self.assertEqual(
            decision.reason,
            "proposed ticket uses an owner-blocked provider",
        )

    def test_nonmatching_market_and_provider_deny_lists_do_not_block(self) -> None:
        decision = self._policy(
            self._goal(
                blocked_markets=frozenset({"market-other"}),
                blocked_providers=frozenset({"provider-other"}),
            )
        ).evaluate(PaperBook("100"), Decimal("1"), context=self._context())

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "allowed")

    def test_owner_sport_deny_list_fails_closed_without_canonical_sport_identity(self) -> None:
        decision = self._policy(
            self._goal(blocked_sports=frozenset({"football"}))
        ).evaluate(PaperBook("100"), Decimal("1"), context=self._context())

        self.assertFalse(decision.allowed)
        self.assertEqual(
            decision.reason,
            "owner sport deny-list cannot be proven without canonical sport identity",
        )

    def test_nondefault_concentration_limits_fail_closed_without_canonical_exposure(self) -> None:
        dimensions = (
            ("max_event_concentration_fraction", "event"),
            ("max_market_concentration_fraction", "market"),
            ("max_provider_concentration_fraction", "provider"),
            ("max_sport_concentration_fraction", "sport"),
        )

        for field_name, dimension in dimensions:
            with self.subTest(field_name=field_name):
                decision = self._policy(
                    self._goal(**{field_name: Decimal("0.99")})
                ).evaluate(PaperBook("100"), Decimal("1"), context=self._context())

                self.assertFalse(decision.allowed)
                self.assertEqual(
                    decision.reason,
                    f"owner {dimension} concentration limit cannot be proven without "
                    "canonical whole-portfolio exposure evidence",
                )


if __name__ == "__main__":
    unittest.main()
