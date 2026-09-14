import unittest
from decimal import Decimal

from autosport.domain import TicketLeg, TicketStatus
from autosport.paper import PaperBook
from autosport.settlement import SettlementEngine


class SettlementOutcomeStateIntegrityTests(unittest.TestCase):
    def test_constructor_rejects_unsupported_outcome(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported outcome: pending"):
            SettlementEngine({"event|winner|selection": "pending"})

    def test_constructor_detaches_caller_owned_mapping(self) -> None:
        raw = {"event|winner|selection": "win"}
        settlement = SettlementEngine(raw)

        raw["event|winner|selection"] = "loss"

        self.assertEqual(settlement.outcomes["event|winner|selection"], "win")

    def test_public_outcome_view_cannot_bypass_record_conflict_guard(self) -> None:
        settlement = SettlementEngine({"event|winner|selection": "win"})

        with self.assertRaises(TypeError):
            settlement.outcomes["event|winner|selection"] = "loss"  # type: ignore[index]

        with self.assertRaisesRegex(ValueError, "conflicting settlement"):
            settlement.record({"event|winner|selection": "loss"})
        self.assertEqual(settlement.outcomes["event|winner|selection"], "win")

    def test_caller_mutation_cannot_turn_valid_win_into_implicit_loss(self) -> None:
        book = PaperBook("100")
        leg = TicketLeg("event", "winner", "selection", Decimal("2"))
        ticket = book.open_ticket([leg], "10")
        raw = {leg.quote_key: "win"}
        settlement = SettlementEngine(raw)

        # Before the repair SettlementEngine retained this dict by reference; an
        # unsupported value was treated as resolved-but-not-winning and therefore
        # reached PaperBook.settle() as an implicit loss.
        raw[leg.quote_key] = "pending"
        settled = settlement.settle_ready(book)

        self.assertEqual(settled, [ticket.ticket_id])
        self.assertEqual(ticket.status, TicketStatus.WON)
        self.assertEqual(ticket.payout, Decimal("20"))
        self.assertEqual(book.balance, Decimal("110"))


if __name__ == "__main__":
    unittest.main()
