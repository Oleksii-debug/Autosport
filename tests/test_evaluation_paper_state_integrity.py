from __future__ import annotations

import unittest
from decimal import DefaultContext, Decimal, Inexact, Overflow, Underflow, localcontext

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

    def test_aliased_ticket_identity_is_rejected_before_durable_metrics(self) -> None:
        book = PaperBook("100")
        leg = TicketLeg("event-alias", "winner", "player-a", Decimal("2"))
        ticket = book.open_ticket([leg], "10", reason="paper-only alias regression")
        book.settle(ticket.ticket_id, {leg.quote_key})
        self.assertEqual(book.balance, Decimal("110"))

        book.tickets["alias"] = ticket
        book.balance = Decimal("120")

        with self.assertRaisesRegex(
            ValueError,
            "virtual bankroll state is invalid for evaluation",
        ):
            evaluate(book)

    def test_subprecision_ticket_economics_are_rejected_instead_of_disappearing(self) -> None:
        book = PaperBook("1")
        leg = TicketLeg("event-tiny", "winner", "player-a", Decimal("2"))
        ticket = PaperTicket(
            ticket_id="tiny-win",
            stake=Decimal("1E-29"),
            legs=(leg,),
            placed_at="2026-09-14T00:00:00+00:00",
            status=TicketStatus.WON,
            payout=Decimal("2E-29"),
        )
        book.tickets[ticket.ticket_id] = ticket
        book.balance = Decimal("1")

        with self.assertRaisesRegex(
            ValueError,
            "virtual bankroll state is invalid for evaluation",
        ):
            evaluate(book)

    def test_exact_large_power_of_ten_book_is_not_rejected_for_representation_rounding(self) -> None:
        book = PaperBook("1E+28")

        summary = evaluate(book)

        self.assertEqual(summary.initial_bankroll, Decimal("1E+28"))
        self.assertEqual(summary.final_balance, Decimal("1E+28"))
        self.assertEqual(summary.committed_stake, Decimal("0"))
        self.assertEqual(summary.settled_stake, Decimal("0"))
        self.assertEqual(summary.net_profit, Decimal("0"))
        self.assertEqual(summary.roi, Decimal("0"))

    def test_out_of_policy_pristine_book_fails_before_unbounded_exact_scaling(self) -> None:
        book = PaperBook(Decimal("1E+1000000000"))

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

    def test_process_default_inexact_trap_cannot_change_valid_roi(self) -> None:
        book = PaperBook("10")
        lost_leg = TicketLeg("event-default-lost", "winner", "player-a", Decimal("2"))
        lost_ticket = book.open_ticket([lost_leg], "2", reason="paper-only loss")
        book.settle(lost_ticket.ticket_id, set())
        won_leg = TicketLeg("event-default-won", "winner", "player-b", Decimal("2"))
        won_ticket = book.open_ticket([won_leg], "1", reason="paper-only win")
        book.settle(won_ticket.ticket_id, {won_leg.quote_key})

        previous_inexact_trap = DefaultContext.traps[Inexact]
        previous_capitals = DefaultContext.capitals
        previous_clamp = DefaultContext.clamp
        try:
            # Context(...) inherits unspecified policy fields from DefaultContext.
            # A valid non-terminating ROI must remain governed only by Autosport's
            # pinned policy even if another library mutates process-global defaults.
            DefaultContext.traps[Inexact] = True
            DefaultContext.capitals = 0
            DefaultContext.clamp = 1

            summary = evaluate(book)
        finally:
            DefaultContext.traps[Inexact] = previous_inexact_trap
            DefaultContext.capitals = previous_capitals
            DefaultContext.clamp = previous_clamp

        self.assertEqual(summary.net_profit, Decimal("-1"))
        self.assertEqual(summary.settled_stake, Decimal("3"))
        self.assertEqual(summary.roi, Decimal("-0.3333333333333333333333333333"))

    def test_valid_open_stake_aggregate_can_exceed_working_precision(self) -> None:
        book = PaperBook("1E+28")
        large_leg = TicketLeg("event-open-large", "winner", "player-a", Decimal("2"))
        small_leg = TicketLeg("event-open-small", "winner", "player-b", Decimal("2"))
        book.open_ticket(
            [large_leg],
            "9999999999999999999999999999",
            reason="exact large open stake",
        )
        book.open_ticket([small_leg], "0.9", reason="exact fractional open stake")
        self.assertEqual(book.balance, Decimal("0.1"))

        summary = evaluate(book)

        self.assertEqual(
            summary.committed_stake,
            Decimal("9999999999999999999999999999.9"),
        )
        self.assertEqual(summary.settled_stake, Decimal("0"))
        self.assertEqual(summary.net_profit, Decimal("0"))
        self.assertEqual(summary.roi, Decimal("0"))

    def test_valid_settled_stake_aggregate_can_exceed_working_precision(self) -> None:
        book = PaperBook("1E+28")
        large_leg = TicketLeg("event-settled-large", "winner", "player-a", Decimal("2"))
        small_leg = TicketLeg("event-settled-small", "winner", "player-b", Decimal("2"))
        large = book.open_ticket(
            [large_leg],
            "9999999999999999999999999999",
            reason="exact large settled stake",
        )
        small = book.open_ticket(
            [small_leg],
            "0.9",
            reason="exact fractional settled stake",
        )
        book.settle(small.ticket_id, set())
        book.settle(large.ticket_id, set())
        self.assertEqual(book.balance, Decimal("0.1"))

        summary = evaluate(book)

        self.assertEqual(summary.committed_stake, Decimal("0"))
        self.assertEqual(
            summary.settled_stake,
            Decimal("9999999999999999999999999999.9"),
        )
        self.assertEqual(
            summary.net_profit,
            Decimal("-9999999999999999999999999999.9"),
        )
        self.assertEqual(summary.roi, Decimal("-1"))
        self.assertEqual(summary.lost, 2)

    def test_canonical_parlay_settlement_rounding_remains_valid(self) -> None:
        book = PaperBook("10000")
        legs = tuple(
            TicketLeg(
                f"event-{index}",
                "winner",
                f"player-{index}",
                Decimal("1.23456789"),
            )
            for index in range(10)
        )
        ticket = book.open_ticket(
            legs,
            "123.45",
            reason="rounded canonical parlay",
        )
        book.settle(ticket.ticket_id, {leg.quote_key for leg in legs})

        summary = evaluate(book)

        self.assertEqual(summary.final_balance, book.balance)
        self.assertEqual(summary.settled_stake, Decimal("123.45"))
        self.assertEqual(
            summary.net_profit,
            Decimal("891.95866691709813439862763"),
        )
        self.assertEqual(summary.won, 1)

    def test_reverse_settlement_chronology_uses_canonical_paper_lifecycle(self) -> None:
        book = PaperBook("10000")
        odds = Decimal("1.234567891234567891234567891")
        first_leg = TicketLeg("event-1", "winner", "alice", odds)
        second_leg = TicketLeg("event-2", "winner", "bob", odds)
        first = book.open_ticket(
            [first_leg],
            "123.45",
            placed_at="2026-09-14T09:00:00+00:00",
        )
        second = book.open_ticket(
            [second_leg],
            "123.45",
            placed_at="2026-09-14T09:01:00+00:00",
        )

        # Canonical PaperBook lifecycle records settlement in the opposite order
        # from ticket insertion. Replaying ticket insertion as settlement chronology
        # differs by one ULP under the product's 28-digit Decimal policy.
        book.settle(second.ticket_id, {second_leg.quote_key})
        book.settle(first.ticket_id, {first_leg.quote_key})
        self.assertEqual(
            book.balance,
            Decimal("10057.91481234581481234581481"),
        )

        summary = evaluate(book)

        self.assertEqual(summary.final_balance, book.balance)
        self.assertEqual(summary.committed_stake, Decimal("0"))
        self.assertEqual(summary.settled_stake, Decimal("246.9"))
        self.assertEqual(
            summary.net_profit,
            Decimal("57.91481234581481234581481"),
        )
        self.assertEqual(summary.won, 2)
        self.assertEqual(summary.lost, 0)
        self.assertEqual(summary.void, 0)

    def test_valid_open_and_settled_economics_are_preserved(self) -> None:
        book = PaperBook("100")
        open_leg = TicketLeg("event-open", "winner", "player-a", Decimal("2"))
        book.open_ticket([open_leg], "10", reason="open paper exposure")
        settled_leg = TicketLeg("event-settled", "winner", "player-b", Decimal("3"))
        settled_ticket = book.open_ticket(
            [settled_leg],
            "5",
            reason="settled paper exposure",
        )
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
