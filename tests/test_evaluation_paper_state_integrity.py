from __future__ import annotations

import unittest
from decimal import Decimal, Overflow, Underflow, localcontext

from autosport.domain import PaperTicket, TicketLeg, TicketStatus
from autosport.evaluation import evaluate
from autosport.paper import PaperBook


class EvaluationPaperStateIntegrityTests(unittest.TestCase):
    def test_finite_inconsistent_balance_is_rejected_before_profit_evidence(self) -> None:
        book = PaperBook("100")
        book.balance = Decimal("99")

        with self.assertRaisesRegex(
            ValueError,
            "virtual bankroll state is invalid for evaluation",
        ):
            evaluate(book)

    def test_noncanonical_ticket_status_is_normalized_to_fail_closed_error(self) -> None:
        book = PaperBook("100")
        leg = TicketLeg("event-1", "winner", "player-a", Decimal("2"))
        ticket = book.open_ticket([leg], "10", reason="paper-only")
        ticket.status = "won"  # type: ignore[assignment]

        with self.assertRaisesRegex(
            ValueError,
            "virtual bankroll state is invalid for evaluation",
        ):
            evaluate(book)

    def test_derived_roi_decimal_range_failure_is_fail_closed_and_context_isolated(self) -> None:
        book = PaperBook(Decimal("1E+999999"))
        leg = TicketLeg("event-1", "winner", "player-a", Decimal("2"))
        ticket = PaperTicket(
            ticket_id="range-ticket",
            stake=Decimal("1E-999999"),
            legs=(leg,),
            placed_at="2026-09-14T00:00:00+00:00",
            status=TicketStatus.WON,
            payout=Decimal("8E+999999"),
        )
        book.tickets[ticket.ticket_id] = ticket
        book.balance = Decimal("9E+999999")

        with localcontext() as caller:
            caller.prec = 7
            caller.traps[Overflow] = False
            caller.traps[Underflow] = False
            caller.clear_flags()

            with self.assertRaisesRegex(
                ValueError,
                "virtual bankroll state is invalid for evaluation",
            ):
                evaluate(book)

            self.assertFalse(caller.flags[Overflow])
            self.assertFalse(caller.flags[Underflow])

    def test_inexact_ledger_profit_identity_is_rejected_instead_of_rounded(self) -> None:
        book = PaperBook("123456789012345678901234567890")

        with self.assertRaisesRegex(
            ValueError,
            "virtual bankroll state is invalid for evaluation",
        ):
            evaluate(book)

    def test_nonterminating_roi_keeps_canonical_decimal_rounding(self) -> None:
        book = PaperBook("10")
        lost_leg = TicketLeg("event-lost", "winner", "player-a", Decimal("2"))
        lost_ticket = book.open_ticket([lost_leg], "2", reason="paper-only loss")
        book.settle(lost_ticket.ticket_id, set())
        won_leg = TicketLeg("event-won", "winner", "player-b", Decimal("2"))
        won_ticket = book.open_ticket([won_leg], "1", reason="paper-only win")
        book.settle(won_ticket.ticket_id, {won_leg.quote_key})

        summary = evaluate(book)

        self.assertEqual(summary.net_profit, Decimal("-1"))
        self.assertEqual(summary.settled_stake, Decimal("3"))
        self.assertEqual(summary.roi, Decimal("-0.3333333333333333333333333333"))

    def test_canonical_parlay_settlement_rounding_remains_valid(self) -> None:
        book = PaperBook("10000")
        legs = tuple(
            TicketLeg(f"event-{index}", "winner", f"player-{index}", Decimal("1.23456789"))
            for index in range(10)
        )
        ticket = book.open_ticket(legs, "123.45", reason="rounded canonical parlay")
        book.settle(ticket.ticket_id, {leg.quote_key for leg in legs})

        summary = evaluate(book)

        self.assertEqual(summary.final_balance, book.balance)
        self.assertEqual(summary.settled_stake, Decimal("123.45"))
        self.assertEqual(summary.net_profit, Decimal("891.95866691709813439862763"))
        self.assertEqual(summary.won, 1)

    def test_valid_open_and_settled_economics_are_preserved(self) -> None:
        book = PaperBook("100")
        open_leg = TicketLeg("event-open", "winner", "player-a", Decimal("2"))
        book.open_ticket([open_leg], "10", reason="open paper exposure")
        settled_leg = TicketLeg("event-settled", "winner", "player-b", Decimal("3"))
        settled_ticket = book.open_ticket([settled_leg], "5", reason="settled paper exposure")
        book.settle(settled_ticket.ticket_id, {settled_leg.quote_key})

        summary = evaluate(book)

        self.assertEqual(summary.initial_bankroll, Decimal("100"))
        self.assertEqual(summary.final_balance, Decimal("100"))
        self.assertEqual(summary.committed_stake, Decimal("10"))
        self.assertEqual(summary.settled_stake, Decimal("5"))
        self.assertEqual(summary.net_profit, Decimal("10"))
        self.assertEqual(summary.roi, Decimal("2"))
        self.assertEqual(summary.won, 1)
        self.assertEqual(summary.lost, 0)
        self.assertEqual(summary.void, 0)


if __name__ == "__main__":
    unittest.main()
