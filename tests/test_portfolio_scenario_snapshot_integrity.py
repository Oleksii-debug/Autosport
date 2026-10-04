from __future__ import annotations

import unittest
from decimal import Decimal
from unittest.mock import patch

import autosport.portfolio as portfolio_module
from autosport.domain import TicketStatus, TicketLeg
from autosport.paper import PaperBook
from autosport.portfolio import PortfolioEngine


class PortfolioScenarioSnapshotIntegrityTests(unittest.TestCase):
    @staticmethod
    def _open_ticket():
        book = PaperBook("100")
        leg = TicketLeg("event-1", "winner", "alice", Decimal("2"))
        ticket = book.open_ticket(
            [leg],
            "10",
            placed_at="2026-09-21T08:00:00+00:00",
        )
        return book, ticket, leg

    def test_exact_analysis_is_stable_if_ticket_settles_after_state_derivation(self) -> None:
        book, ticket, leg = self._open_ticket()

        class SettlingExactEngine(PortfolioEngine):
            def _exact_profits(self, tickets, groups, ungrouped):
                book.settle(
                    ticket.ticket_id,
                    {leg.quote_key},
                    settled_at="2026-09-21T08:00:01+00:00",
                )
                return super()._exact_profits(tickets, groups, ungrouped)

        report = SettlingExactEngine().analyse([ticket])

        self.assertEqual(ticket.status, TicketStatus.WON)
        self.assertEqual(report.mode, "exact")
        self.assertEqual(report.scenario_count, 2)
        self.assertEqual(report.worst_case, Decimal("-10"))
        self.assertEqual(report.best_case, Decimal("10"))
        self.assertEqual(report.mean_case, Decimal("0"))

    def test_sampled_analysis_is_stable_if_ticket_settles_after_state_derivation(self) -> None:
        book, ticket, leg = self._open_ticket()

        class SettlingSampleEngine(PortfolioEngine):
            def _sample_profits(self, tickets, groups, ungrouped):
                book.settle(
                    ticket.ticket_id,
                    {leg.quote_key},
                    settled_at="2026-09-21T08:00:01+00:00",
                )
                return super()._sample_profits(tickets, groups, ungrouped)

        report = SettlingSampleEngine(
            max_exact_states=1,
            sample_count=8,
            seed=7,
        ).analyse([ticket])

        self.assertEqual(ticket.status, TicketStatus.WON)
        self.assertEqual(report.mode, "approximate")
        self.assertEqual(report.scenario_count, 8)
        self.assertEqual(report.worst_case, Decimal("-10"))
        self.assertEqual(report.best_case, Decimal("10"))

    def test_direct_scenario_profit_uses_frozen_open_ticket_cut(self) -> None:
        book, ticket, leg = self._open_ticket()
        original_profit = portfolio_module._scenario_profit_in_context

        def settle_source_then_calculate(tickets, winning_quote_keys):
            book.settle(
                ticket.ticket_id,
                {leg.quote_key},
                settled_at="2026-09-21T08:00:01+00:00",
            )
            return original_profit(tickets, winning_quote_keys)

        with patch(
            "autosport.portfolio._scenario_profit_in_context",
            side_effect=settle_source_then_calculate,
        ):
            profit = PortfolioEngine.scenario_profit(
                [ticket],
                {leg.quote_key},
            )

        self.assertEqual(ticket.status, TicketStatus.WON)
        self.assertEqual(
            profit,
            Decimal("10"),
            "direct scenario profit must use the OPEN-ticket cut captured before evaluation",
        )

    def test_direct_scenario_profit_freezes_winner_set_before_ticket_snapshot(self) -> None:
        _book, ticket, leg = self._open_ticket()
        winners = {leg.quote_key}
        original_snapshot = portfolio_module._snapshot_open_tickets_for_analysis

        def snapshot_then_mutate_winners(tickets):
            snapshot = original_snapshot(tickets)
            winners.clear()
            return snapshot

        with patch(
            "autosport.portfolio._snapshot_open_tickets_for_analysis",
            side_effect=snapshot_then_mutate_winners,
        ):
            profit = PortfolioEngine.scenario_profit([ticket], winners)

        self.assertEqual(winners, set())
        self.assertEqual(
            profit,
            Decimal("10"),
            "scenario winners must be frozen before mutable ticket snapshot work",
        )

    def test_direct_scenario_profit_normalizes_winners_with_paperbook_contract(self) -> None:
        _book, ticket, leg = self._open_ticket()

        self.assertEqual(
            PortfolioEngine.scenario_profit([ticket], (leg.quote_key,)),
            Decimal("10"),
        )
        with self.assertRaisesRegex(ValueError, "collection of quote keys"):
            PortfolioEngine.scenario_profit([ticket], leg.quote_key)
        with self.assertRaisesRegex(ValueError, "non-empty string"):
            PortfolioEngine.scenario_profit([ticket], {""})

    def test_direct_scenario_profit_rejects_non_positive_ticket_stake(self) -> None:
        leg = TicketLeg("event", "winner", "alice", Decimal("2"))
        ticket = portfolio_module.PaperTicket(
            ticket_id="synthetic-negative-stake",
            stake=Decimal("-10"),
            legs=(leg,),
            placed_at="2026-09-21T08:00:00+00:00",
        )

        with self.assertRaisesRegex(ValueError, "stake must be positive"):
            PortfolioEngine.scenario_profit([ticket], {leg.quote_key})

    def test_direct_scenario_profit_rejects_empty_ticket_legs(self) -> None:
        ticket = portfolio_module.PaperTicket(
            ticket_id="synthetic-empty-ticket",
            stake=Decimal("10"),
            legs=(),
            placed_at="2026-09-21T08:00:00+00:00",
        )

        with self.assertRaisesRegex(ValueError, "requires at least one canonical leg"):
            PortfolioEngine.scenario_profit([ticket], set())

    def test_direct_scenario_profit_rejects_lay_leg_back_math_bypass(self) -> None:
        lay_leg = TicketLeg(
            "event",
            "winner",
            "alice",
            Decimal("2"),
            exchange_side="lay",
        )
        ticket = portfolio_module.PaperTicket(
            ticket_id="synthetic-lay-ticket",
            stake=Decimal("10"),
            legs=(lay_leg,),
            placed_at="2026-09-21T08:00:00+00:00",
        )

        with self.assertRaisesRegex(
            ValueError,
            "LAY economic materialization is not supported",
        ):
            PortfolioEngine.scenario_profit([ticket], {lay_leg.quote_key})

    def test_direct_scenario_profit_rejects_duplicate_quote_identity(self) -> None:
        leg = TicketLeg("event", "winner", "alice", Decimal("2"))
        ticket = portfolio_module.PaperTicket(
            ticket_id="synthetic-duplicate-ticket",
            stake=Decimal("10"),
            legs=(leg, leg),
            placed_at="2026-09-21T08:00:00+00:00",
        )

        with self.assertRaisesRegex(ValueError, "duplicate quote_key leg"):
            PortfolioEngine.scenario_profit([ticket], {leg.quote_key})

    def test_portfolio_snapshot_rejects_non_enum_ticket_status(self) -> None:
        leg = TicketLeg("event", "winner", "alice", Decimal("2"))
        ticket = portfolio_module.PaperTicket(
            ticket_id="synthetic-bad-status",
            stake=Decimal("10"),
            legs=(leg,),
            placed_at="2026-09-21T08:00:00+00:00",
        )
        ticket.status = "open"

        with self.assertRaisesRegex(ValueError, "status must be exact TicketStatus"):
            PortfolioEngine().analyse([ticket])

    def test_portfolio_snapshot_rejects_duplicate_ticket_identity(self) -> None:
        leg_a = TicketLeg("event-a", "winner", "alice", Decimal("2"))
        leg_b = TicketLeg("event-b", "winner", "bob", Decimal("3"))
        first = portfolio_module.PaperTicket(
            ticket_id="duplicate-ticket",
            stake=Decimal("10"),
            legs=(leg_a,),
            placed_at="2026-09-21T08:00:00+00:00",
        )
        second = portfolio_module.PaperTicket(
            ticket_id="duplicate-ticket",
            stake=Decimal("20"),
            legs=(leg_b,),
            placed_at="2026-09-21T08:00:01+00:00",
        )

        with self.assertRaisesRegex(ValueError, "ticket_id values must be unique"):
            PortfolioEngine().analyse([first, second])

    def test_affected_tickets_uses_validated_snapshot_boundary(self) -> None:
        _book, ticket, leg = self._open_ticket()

        self.assertEqual(
            PortfolioEngine.affected_tickets([ticket], leg.quote_key),
            [ticket.ticket_id],
        )
        with self.assertRaisesRegex(ValueError, "canonical non-empty text"):
            PortfolioEngine.affected_tickets([ticket], "")

        ticket.status = "open"
        with self.assertRaisesRegex(ValueError, "status must be exact TicketStatus"):
            PortfolioEngine.affected_tickets([ticket], leg.quote_key)

    def test_snapshot_fails_closed_if_settlement_crosses_capture_window(self) -> None:
        book = PaperBook("100")
        first_leg = TicketLeg("event-1", "winner", "alice", Decimal("2"))
        second_leg = TicketLeg("event-2", "winner", "bob", Decimal("2"))
        first = book.open_ticket(
            [first_leg],
            "10",
            placed_at="2026-09-21T08:00:00+00:00",
        )
        second = book.open_ticket(
            [second_leg],
            "10",
            placed_at="2026-09-21T08:00:00+00:00",
        )
        original_validate = (
            portfolio_module._validate_open_ticket_economics_for_analysis
        )
        validations = 0

        def validate_then_settle(ticket_id, stake, legs):
            nonlocal validations
            original_validate(ticket_id, stake, legs)
            validations += 1
            if validations == 1:
                book.settle(
                    first.ticket_id,
                    {first_leg.quote_key},
                    settled_at="2026-09-21T08:00:01+00:00",
                )
                book.settle(
                    second.ticket_id,
                    {second_leg.quote_key},
                    settled_at="2026-09-21T08:00:02+00:00",
                )

        with patch(
            "autosport.portfolio._validate_open_ticket_economics_for_analysis",
            side_effect=validate_then_settle,
        ):
            with self.assertRaisesRegex(
                ValueError,
                "portfolio ticket changed during snapshot",
            ):
                PortfolioEngine().analyse([first, second])

        self.assertEqual(first.status, TicketStatus.WON)
        self.assertEqual(second.status, TicketStatus.WON)


    def test_direct_scenario_profit_rejects_open_ticket_with_settlement_residue(self) -> None:
        _book, ticket, leg = self._open_ticket()
        ticket.payout = Decimal("5")

        with self.assertRaisesRegex(ValueError, "open ticket .* payout must be zero"):
            PortfolioEngine.scenario_profit([ticket], {leg.quote_key})

        ticket.payout = Decimal("0")
        ticket.settled_at = "2026-09-21T08:00:01+00:00"
        with self.assertRaisesRegex(ValueError, "open ticket .* cannot have settled_at"):
            PortfolioEngine.scenario_profit([ticket], {leg.quote_key})

    def test_affected_tickets_rejects_open_ticket_with_settlement_residue(self) -> None:
        _book, ticket, leg = self._open_ticket()
        ticket.payout = Decimal("1")

        with self.assertRaisesRegex(ValueError, "open ticket .* payout must be zero"):
            PortfolioEngine.affected_tickets([ticket], leg.quote_key)


    def test_snapshot_rejects_paperticket_subclass_before_attribute_dispatch(self) -> None:
        _book, ticket, leg = self._open_ticket()
        canonical_ticket_type = type(ticket)

        class HostilePaperTicket(canonical_ticket_type):
            def __getattribute__(self, name):
                if name in {
                    "ticket_id",
                    "stake",
                    "legs",
                    "placed_at",
                    "status",
                    "payout",
                    "settled_at",
                    "provider_source_ids",
                }:
                    raise AssertionError(
                        "PaperTicket subclass attribute dispatch executed"
                    )
                return super().__getattribute__(name)

        hostile = HostilePaperTicket(
            ticket_id="hostile-ticket-subclass",
            stake=Decimal("10"),
            legs=(leg,),
            placed_at="2026-09-21T08:00:00+00:00",
        )
        with self.assertRaisesRegex(
            ValueError,
            "ticket must be exact PaperTicket",
        ):
            PortfolioEngine.scenario_profit([hostile], {leg.quote_key})

    def test_ticket_and_status_global_rebind_cannot_redirect_snapshot_root(self) -> None:
        _book, ticket, leg = self._open_ticket()

        class PoisonPaperTicket:
            def __new__(cls, *args, **kwargs):
                raise AssertionError("rebound PaperTicket constructor executed")

        class PoisonTicketStatus:
            OPEN = object()

        with patch.object(
            portfolio_module,
            "PaperTicket",
            PoisonPaperTicket,
        ), patch.object(
            portfolio_module,
            "TicketStatus",
            PoisonTicketStatus,
        ):
            self.assertEqual(
                PortfolioEngine.scenario_profit([ticket], {leg.quote_key}),
                Decimal("10"),
            )
            self.assertEqual(
                PortfolioEngine.scenario_profit_settlements(
                    [ticket],
                    {leg.quote_key: "win"},
                ),
                Decimal("10"),
            )
            report = PortfolioEngine().analyse([ticket])
            self.assertEqual(report.worst_case, Decimal("-10"))
            self.assertEqual(report.best_case, Decimal("10"))

    def test_direct_scenario_profit_rejects_decimal_subclass_before_virtual_dispatch(self) -> None:
        class HostileDecimal(Decimal):
            def is_finite(self):
                raise AssertionError("hostile Decimal is_finite executed")

        leg = TicketLeg("event", "winner", "alice", Decimal("2"))
        ticket = portfolio_module.PaperTicket(
            ticket_id="synthetic-hostile-decimal",
            stake=HostileDecimal("10"),
            legs=(leg,),
            placed_at="2026-09-21T08:00:00+00:00",
        )

        with self.assertRaisesRegex(ValueError, "stake must be a finite Decimal"):
            PortfolioEngine.scenario_profit([ticket], {leg.quote_key})


    def test_decimal_global_rebind_cannot_replace_portfolio_money_root(self) -> None:
        class HostileDecimal(Decimal):
            def is_finite(self):
                raise AssertionError("rebound Decimal is_finite executed")

        leg = TicketLeg("event-hostile-root", "winner", "alice", Decimal("2"))
        hostile_ticket = portfolio_module.PaperTicket(
            ticket_id="synthetic-rebound-decimal",
            stake=HostileDecimal("10"),
            legs=(leg,),
            placed_at="2026-09-21T08:00:00+00:00",
        )
        with patch.object(portfolio_module, "Decimal", HostileDecimal):
            with self.assertRaisesRegex(
                ValueError,
                "stake must be a finite Decimal",
            ):
                PortfolioEngine.scenario_profit(
                    [hostile_ticket],
                    {leg.quote_key},
                )

        _book, ticket, exact_leg = self._open_ticket()

        class PoisonDecimal:
            def __new__(cls, *args, **kwargs):
                raise AssertionError("rebound Decimal constructor executed")

        with patch.object(portfolio_module, "Decimal", PoisonDecimal):
            self.assertEqual(
                PortfolioEngine.scenario_profit(
                    [ticket],
                    {exact_leg.quote_key},
                ),
                Decimal("10"),
            )
            self.assertEqual(
                PortfolioEngine.scenario_profit_settlements(
                    [ticket],
                    {exact_leg.quote_key: "win"},
                ),
                Decimal("10"),
            )
            report = PortfolioEngine().analyse([ticket])
            self.assertEqual(report.worst_case, Decimal("-10"))
            self.assertEqual(report.best_case, Decimal("10"))
            self.assertEqual(report.mean_case, Decimal("0"))


    def test_paperbook_global_rebind_cannot_replace_portfolio_economic_authority(self) -> None:
        _book, ticket, leg = self._open_ticket()

        class PoisonPaperBook:
            @staticmethod
            def _normalize_resolution_keys(*args, **kwargs):
                raise AssertionError("rebound PaperBook normalizer executed")

            @classmethod
            def _validate_ticket_leg(cls, *args, **kwargs):
                raise AssertionError("rebound PaperBook leg validator executed")

            @classmethod
            def _validate_placed_at(cls, *args, **kwargs):
                raise AssertionError("rebound PaperBook timestamp validator executed")

            @classmethod
            def _settlement_result(cls, *args, **kwargs):
                raise AssertionError("rebound PaperBook settlement authority executed")

        with patch.object(portfolio_module, "PaperBook", PoisonPaperBook):
            self.assertEqual(
                PortfolioEngine.scenario_profit(
                    [ticket],
                    {leg.quote_key},
                ),
                Decimal("10"),
            )
            self.assertEqual(
                PortfolioEngine.scenario_profit_settlements(
                    [ticket],
                    {leg.quote_key: "win"},
                ),
                Decimal("10"),
            )
            report = PortfolioEngine().analyse([ticket])
            self.assertEqual(report.worst_case, Decimal("-10"))
            self.assertEqual(report.best_case, Decimal("10"))


    def test_nested_leg_odds_are_detached_before_direct_scenario_evaluation(self) -> None:
        _book, ticket, leg = self._open_ticket()
        original_profit = portfolio_module._scenario_profit_in_context

        def mutate_source_leg_then_calculate(tickets, winning_quote_keys):
            object.__setattr__(leg, "locked_odds", Decimal("100"))
            return original_profit(tickets, winning_quote_keys)

        with patch(
            "autosport.portfolio._scenario_profit_in_context",
            side_effect=mutate_source_leg_then_calculate,
        ):
            profit = PortfolioEngine.scenario_profit([ticket], {leg.quote_key})

        self.assertEqual(leg.locked_odds, Decimal("100"))
        self.assertEqual(
            profit,
            Decimal("10"),
            "nested TicketLeg odds must be detached from the mutable source graph",
        )

    def test_nested_leg_quote_identity_is_detached_before_direct_evaluation(self) -> None:
        _book, ticket, leg = self._open_ticket()
        winning_quote_key = leg.quote_key
        original_profit = portfolio_module._scenario_profit_in_context

        def mutate_source_leg_then_calculate(tickets, winning_quote_keys):
            object.__setattr__(leg, "event_id", "event-mutated-after-snapshot")
            return original_profit(tickets, winning_quote_keys)

        with patch(
            "autosport.portfolio._scenario_profit_in_context",
            side_effect=mutate_source_leg_then_calculate,
        ):
            profit = PortfolioEngine.scenario_profit([ticket], {winning_quote_key})

        self.assertNotEqual(leg.quote_key, winning_quote_key)
        self.assertEqual(
            profit,
            Decimal("10"),
            "nested quote identity must be frozen before scenario evaluation",
        )

    def test_nested_leg_mutation_crossing_capture_window_fails_closed(self) -> None:
        _book, ticket, leg = self._open_ticket()
        original_validate = (
            portfolio_module._validate_open_ticket_economics_for_analysis
        )
        mutated = False

        def validate_then_mutate_source_leg(ticket_id, stake, legs):
            nonlocal mutated
            original_validate(ticket_id, stake, legs)
            if not mutated:
                mutated = True
                object.__setattr__(leg, "locked_odds", Decimal("3"))

        with patch(
            "autosport.portfolio._validate_open_ticket_economics_for_analysis",
            side_effect=validate_then_mutate_source_leg,
        ):
            with self.assertRaisesRegex(
                ValueError,
                "portfolio ticket changed during snapshot",
            ):
                PortfolioEngine.scenario_profit([ticket], {leg.quote_key})

        self.assertEqual(leg.locked_odds, Decimal("3"))

    def test_ticket_leg_global_rebind_cannot_redirect_nested_snapshot_root(self) -> None:
        _book, ticket, leg = self._open_ticket()

        class PoisonTicketLeg:
            def __new__(cls, *args, **kwargs):
                raise AssertionError("rebound TicketLeg constructor executed")

        with patch.object(portfolio_module, "TicketLeg", PoisonTicketLeg):
            self.assertEqual(
                PortfolioEngine.scenario_profit([ticket], {leg.quote_key}),
                Decimal("10"),
            )
            self.assertEqual(
                PortfolioEngine.scenario_profit_settlements(
                    [ticket],
                    {leg.quote_key: "win"},
                ),
                Decimal("10"),
            )
            report = PortfolioEngine().analyse([ticket])
            self.assertEqual(report.worst_case, Decimal("-10"))
            self.assertEqual(report.best_case, Decimal("10"))


if __name__ == "__main__":
    unittest.main()
