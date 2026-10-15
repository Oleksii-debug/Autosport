import unittest
from decimal import Decimal

from autosport.domain import TicketLeg, TicketStatus
from autosport.paper import PaperBook
from autosport.settlement import SettlementEngine


class SettlementBatchAtomicityTests(unittest.TestCase):
    @staticmethod
    def _single_ticket_book() -> tuple[PaperBook, TicketLeg, object]:
        book = PaperBook("100")
        leg = TicketLeg("event-1", "winner", "alice", Decimal("2"))
        ticket = book.open_ticket(
            [leg],
            "10",
            placed_at="2026-09-14T19:30:00+00:00",
        )
        return book, leg, ticket

    def _assert_unchanged_open_ticket(self, book, ticket, before_balance, before_lifecycle) -> None:
        self.assertEqual(book.balance, before_balance)
        self.assertIs(ticket.status, TicketStatus.OPEN)
        self.assertEqual(ticket.payout, Decimal("0"))
        self.assertEqual(book._lifecycle, before_lifecycle)

    def test_later_ready_failure_leaves_entire_batch_unsettled(self) -> None:
        book = PaperBook("9E+999999")
        first_leg = TicketLeg("event-1", "winner", "alice", Decimal("2"))
        second_leg = TicketLeg("event-2", "winner", "bob", Decimal("2"))
        first = book.open_ticket(
            [first_leg],
            "1E+999999",
            placed_at="2026-09-14T19:30:00+00:00",
        )
        second = book.open_ticket(
            [second_leg],
            "4E+999999",
            placed_at="2026-09-14T19:31:00+00:00",
        )
        settlement = SettlementEngine(
            {
                first_leg.quote_key: "win",
                second_leg.quote_key: "win",
            }
        )
        before_balance = book.balance
        before_lifecycle = list(book._lifecycle)

        with self.assertRaisesRegex(
            ValueError, "settlement arithmetic is not representable"
        ):
            settlement.settle_ready(book)

        self.assertEqual(book.balance, before_balance)
        self.assertIs(first.status, TicketStatus.OPEN)
        self.assertEqual(first.payout, Decimal("0"))
        self.assertIs(second.status, TicketStatus.OPEN)
        self.assertEqual(second.payout, Decimal("0"))
        self.assertEqual(book._lifecycle, before_lifecycle)

    def test_noncanonical_later_ticket_identity_fails_before_batch_mutation(self) -> None:
        book = PaperBook("100")
        first_leg = TicketLeg("event-1", "winner", "alice", Decimal("2"))
        second_leg = TicketLeg("event-2", "winner", "bob", Decimal("2"))
        first = book.open_ticket(
            [first_leg],
            "10",
            placed_at="2026-09-14T19:30:00+00:00",
        )
        second = book.open_ticket(
            [second_leg],
            "10",
            placed_at="2026-09-14T19:31:00+00:00",
        )
        settlement = SettlementEngine(
            {
                first_leg.quote_key: "win",
                second_leg.quote_key: "win",
            }
        )
        before_balance = book.balance
        before_lifecycle = list(book._lifecycle)
        second.ticket_id = "mutated-ticket-id"

        with self.assertRaisesRegex(ValueError, "mapping key must match ticket_id"):
            settlement.settle_ready(book)

        self.assertEqual(book.balance, before_balance)
        self.assertIs(first.status, TicketStatus.OPEN)
        self.assertEqual(first.payout, Decimal("0"))
        self.assertIs(second.status, TicketStatus.OPEN)
        self.assertEqual(second.payout, Decimal("0"))
        self.assertEqual(book._lifecycle, before_lifecycle)

    def test_overridable_paperbook_cannot_break_batch_atomicity(self) -> None:
        class PartiallyApplyingPaperBook(PaperBook):
            def __init__(self, initial_bankroll: str) -> None:
                super().__init__(initial_bankroll)
                self.settle_calls = 0

            def settle(self, ticket_id, winning_quote_keys, void_quote_keys=None):
                self.settle_calls += 1
                if self.settle_calls == 2:
                    raise RuntimeError("subclass interrupted batch apply")
                return super().settle(ticket_id, winning_quote_keys, void_quote_keys)

        book = PartiallyApplyingPaperBook("100")
        first_leg = TicketLeg("event-1", "winner", "alice", Decimal("2"))
        second_leg = TicketLeg("event-2", "winner", "bob", Decimal("2"))
        first = book.open_ticket(
            [first_leg],
            "10",
            placed_at="2026-09-14T19:30:00+00:00",
        )
        second = book.open_ticket(
            [second_leg],
            "10",
            placed_at="2026-09-14T19:31:00+00:00",
        )
        settlement = SettlementEngine(
            {
                first_leg.quote_key: "win",
                second_leg.quote_key: "win",
            }
        )
        before_balance = book.balance
        before_lifecycle = list(book._lifecycle)

        with self.assertRaisesRegex(ValueError, "settlement book must be an exact PaperBook"):
            settlement.settle_ready(book)

        self.assertEqual(book.settle_calls, 0)
        self.assertEqual(book.balance, before_balance)
        self.assertIs(first.status, TicketStatus.OPEN)
        self.assertEqual(first.payout, Decimal("0"))
        self.assertIs(second.status, TicketStatus.OPEN)
        self.assertEqual(second.payout, Decimal("0"))
        self.assertEqual(book._lifecycle, before_lifecycle)

    def test_instance_shadowed_settle_cannot_reenter_apply_phase(self) -> None:
        book = PaperBook("100")
        first_leg = TicketLeg("event-1", "winner", "alice", Decimal("2"))
        second_leg = TicketLeg("event-2", "winner", "bob", Decimal("2"))
        first = book.open_ticket(
            [first_leg],
            "10",
            placed_at="2026-09-14T19:30:00+00:00",
        )
        second = book.open_ticket(
            [second_leg],
            "10",
            placed_at="2026-09-14T19:31:00+00:00",
        )
        settle_calls = 0

        def poisoned_settle(ticket_id, winning_quote_keys, void_quote_keys=None):
            nonlocal settle_calls
            settle_calls += 1
            if settle_calls == 2:
                raise RuntimeError("instance shadow interrupted batch apply")
            return PaperBook.settle(book, ticket_id, winning_quote_keys, void_quote_keys)

        book.settle = poisoned_settle  # type: ignore[method-assign]
        settlement = SettlementEngine(
            {
                first_leg.quote_key: "win",
                second_leg.quote_key: "win",
            }
        )

        before_balance = book.balance
        before_lifecycle = list(book._lifecycle)

        with self.assertRaisesRegex(
            ValueError, "settlement book mutation helpers must not be shadowed"
        ):
            settlement.settle_ready(book)

        self.assertEqual(settle_calls, 0)
        self.assertEqual(book.balance, before_balance)
        self.assertIs(first.status, TicketStatus.OPEN)
        self.assertEqual(first.payout, Decimal("0"))
        self.assertIs(second.status, TicketStatus.OPEN)
        self.assertEqual(second.payout, Decimal("0"))
        self.assertEqual(book._lifecycle, before_lifecycle)

    def test_instance_shadowed_settlement_helper_fails_before_batch_mutation(self) -> None:
        book = PaperBook("100")
        first_leg = TicketLeg("event-1", "winner", "alice", Decimal("2"))
        second_leg = TicketLeg("event-2", "winner", "bob", Decimal("2"))
        first = book.open_ticket(
            [first_leg],
            "10",
            placed_at="2026-09-14T19:30:00+00:00",
        )
        second = book.open_ticket(
            [second_leg],
            "10",
            placed_at="2026-09-14T19:31:00+00:00",
        )
        helper_calls = 0

        def poisoned_result(ticket, balance, winning_quote_keys, void_quote_keys):
            nonlocal helper_calls
            helper_calls += 1
            if helper_calls == 2:
                raise RuntimeError("instance helper interrupted batch apply")
            return PaperBook._settlement_result(
                ticket,
                balance,
                winning_quote_keys,
                void_quote_keys,
            )

        book._settlement_result = poisoned_result  # type: ignore[method-assign]
        settlement = SettlementEngine(
            {
                first_leg.quote_key: "win",
                second_leg.quote_key: "win",
            }
        )
        before_balance = book.balance
        before_lifecycle = list(book._lifecycle)

        with self.assertRaisesRegex(
            ValueError, "settlement book mutation helpers must not be shadowed"
        ):
            settlement.settle_ready(book)

        self.assertEqual(helper_calls, 0)
        self.assertEqual(book.balance, before_balance)
        self.assertIs(first.status, TicketStatus.OPEN)
        self.assertEqual(first.payout, Decimal("0"))
        self.assertIs(second.status, TicketStatus.OPEN)
        self.assertEqual(second.payout, Decimal("0"))
        self.assertEqual(book._lifecycle, before_lifecycle)

    def test_invalid_public_outcome_state_cannot_become_false_loss(self) -> None:
        book, leg, ticket = self._single_ticket_book()
        settlement = SettlementEngine({leg.quote_key: "corrupt"})
        before_balance = book.balance
        before_lifecycle = list(book._lifecycle)

        with self.assertRaisesRegex(ValueError, "unsupported outcome: corrupt"):
            settlement.settle_ready(book)

        self._assert_unchanged_open_ticket(book, ticket, before_balance, before_lifecycle)

    def test_duplicate_iterable_constructor_state_cannot_collapse_to_false_loss(self) -> None:
        book, leg, ticket = self._single_ticket_book()
        settlement = SettlementEngine(  # type: ignore[arg-type]
            [(leg.quote_key, "win"), (leg.quote_key, "loss")]
        )
        before_balance = book.balance
        before_lifecycle = list(book._lifecycle)

        with self.assertRaisesRegex(ValueError, "settlement outcomes must be an exact dict"):
            settlement.settle_ready(book)

        self._assert_unchanged_open_ticket(book, ticket, before_balance, before_lifecycle)

    def test_coercible_non_mapping_constructor_state_is_rejected_without_mutation(self) -> None:
        book, leg, ticket = self._single_ticket_book()
        settlement = SettlementEngine([(leg.quote_key, "win")])  # type: ignore[arg-type]
        before_balance = book.balance
        before_lifecycle = list(book._lifecycle)

        with self.assertRaisesRegex(ValueError, "settlement outcomes must be an exact dict"):
            settlement.settle_ready(book)

        self._assert_unchanged_open_ticket(book, ticket, before_balance, before_lifecycle)

    def test_invalid_quote_key_shape_is_rejected_without_mutation(self) -> None:
        book, _, ticket = self._single_ticket_book()
        settlement = SettlementEngine({" quote-key ": "win"})
        before_balance = book.balance
        before_lifecycle = list(book._lifecycle)

        with self.assertRaisesRegex(
            ValueError, "settlement quote key must be a non-empty trimmed string"
        ):
            settlement.settle_ready(book)

        self._assert_unchanged_open_ticket(book, ticket, before_balance, before_lifecycle)

    def test_canonical_dict_still_settles_normally(self) -> None:
        book, leg, ticket = self._single_ticket_book()
        settlement = SettlementEngine({leg.quote_key: "win"})

        self.assertEqual(settlement.settle_ready(book), [ticket.ticket_id])
        self.assertIs(ticket.status, TicketStatus.WON)
        self.assertEqual(ticket.payout, Decimal("20"))
        self.assertEqual(book.balance, Decimal("110"))


if __name__ == "__main__":
    unittest.main()
