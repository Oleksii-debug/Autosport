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
        canonical_ticket_type = type(first)
        copies = 0

        def copy_then_settle(*args, **kwargs):
            nonlocal copies
            snapshot = canonical_ticket_type(*args, **kwargs)
            copies += 1
            if copies == 1:
                # The unsafe interleaving is deterministic:
                # first was copied OPEN, then first and second settle before
                # the second source ticket is inspected.  A naive one-pass
                # copy would publish {first OPEN, second absent}, a state that
                # never existed at one instant.
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
            return snapshot

        with patch(
            "autosport.portfolio.PaperTicket",
            side_effect=copy_then_settle,
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


if __name__ == "__main__":
    unittest.main()
