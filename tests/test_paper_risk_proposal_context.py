import unittest
from decimal import Decimal

from autosport.domain import TicketLeg
from autosport.paper import PaperBook
from autosport.risk import PaperRiskPolicy, ProposedTicketRiskContext


class ProposedTicketRiskContextTests(unittest.TestCase):
    @staticmethod
    def _policy() -> PaperRiskPolicy:
        return PaperRiskPolicy(
            max_ticket_fraction=Decimal("0.50"),
            max_committed_fraction=Decimal("0.90"),
            minimum_cash_reserve_fraction=Decimal("0"),
        )

    @staticmethod
    def _leg(
        event_id: str = "event-1",
        market_id: str = "market-1",
        selection_id: str = "selection-1",
    ) -> TicketLeg:
        return TicketLeg(
            event_id,
            market_id,
            selection_id,
            Decimal("2.00"),
        )

    def test_legacy_evaluate_call_remains_compatible(self) -> None:
        decision = self._policy().evaluate(PaperBook("100"), "10")

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "allowed")

    def test_valid_context_reuses_canonical_leg_identity_without_mutation(self) -> None:
        book = PaperBook("100")
        leg = self._leg()
        context = ProposedTicketRiskContext(
            legs=(leg,),
            bankroll_id="bankroll-owner-1",
            currency="EUR",
            measurement_session_id="session-1",
            measurement_day="2026-09-16",
            source_id="source-1",
            quote_observed_ts="2026-09-16T16:00:00Z",
            quote_source_ts="2026-09-16T15:59:59Z",
            data_quality_evidence="quality-proof-1",
            slippage=Decimal("0.01"),
        )

        decision = self._policy().evaluate(book, "10", context)

        self.assertTrue(decision.allowed)
        self.assertIs(context.legs[0], leg)
        self.assertEqual(context.legs[0].event_id, "event-1")
        self.assertEqual(context.legs[0].market_id, "market-1")
        self.assertEqual(context.legs[0].selection_id, "selection-1")
        self.assertEqual(book.balance, Decimal("100"))
        self.assertEqual(book.tickets, {})

    def test_parlay_leg_count_is_derived_from_canonical_leg_tuple(self) -> None:
        context = ProposedTicketRiskContext(
            legs=(
                self._leg(),
                self._leg("event-2", "market-2", "selection-2"),
            )
        )

        self.assertEqual(context.parlay_leg_count, 2)

    def test_empty_context_fails_closed_instead_of_inventing_proposal_identity(self) -> None:
        context = ProposedTicketRiskContext(legs=())

        decision = self._policy().evaluate(PaperBook("100"), "10", context)

        self.assertFalse(decision.allowed)
        self.assertEqual(
            decision.reason,
            "proposed-ticket risk context is invalid",
        )

    def test_noncanonical_leg_identity_fails_closed_without_normalization(self) -> None:
        leg = self._leg(event_id=" event-1")
        context = ProposedTicketRiskContext(legs=(leg,))

        decision = self._policy().evaluate(PaperBook("100"), "10", context)

        self.assertFalse(decision.allowed)
        self.assertEqual(
            decision.reason,
            "proposed-ticket risk context is invalid",
        )
        self.assertEqual(leg.event_id, " event-1")

    def test_duplicate_proposed_leg_identity_is_rejected_as_ambiguous(self) -> None:
        leg = self._leg()
        context = ProposedTicketRiskContext(legs=(leg, leg))

        decision = self._policy().evaluate(PaperBook("100"), "10", context)

        self.assertFalse(decision.allowed)
        self.assertEqual(
            decision.reason,
            "proposed-ticket risk context is invalid",
        )

    def test_invalid_optional_identity_fails_closed_without_inference(self) -> None:
        context = ProposedTicketRiskContext(
            legs=(self._leg(),),
            source_id=" ",
        )

        decision = self._policy().evaluate(PaperBook("100"), "10", context)

        self.assertFalse(decision.allowed)
        self.assertEqual(
            decision.reason,
            "proposed-ticket risk context is invalid",
        )
        self.assertIsNone(context.bankroll_id)
        self.assertIsNone(context.currency)

    def test_invalid_slippage_evidence_fails_closed(self) -> None:
        context = ProposedTicketRiskContext(
            legs=(self._leg(),),
            slippage=Decimal("NaN"),
        )

        decision = self._policy().evaluate(PaperBook("100"), "10", context)

        self.assertFalse(decision.allowed)
        self.assertEqual(
            decision.reason,
            "proposed-ticket risk context is invalid",
        )


if __name__ == "__main__":
    unittest.main()
