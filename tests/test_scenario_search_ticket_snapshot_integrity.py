from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

from autosport.domain import TicketLeg, TicketStatus
from autosport.market_outcomes import (
    OutcomeAuthorityStatus,
    assess_betfair_historical_market_definition_authority,
)
from autosport.paper import PaperBook
from autosport.portfolio import PortfolioEngine
from autosport.scenario_search import (
    ScenarioGroup,
    ScenarioOutcome,
    ScenarioSearchEngine,
)


class ScenarioSearchTicketSnapshotIntegrityTests(unittest.TestCase):
    SOURCE_ID = "betfair_exchange_historical"
    DECISION_AS_OF = datetime(2026, 9, 18, 15, 0, 2, tzinfo=timezone.utc)

    @staticmethod
    def _winner_group(home: TicketLeg, away: TicketLeg) -> list[ScenarioGroup]:
        return [
            ScenarioGroup(
                "match-odds",
                (
                    ScenarioOutcome(home.quote_key),
                    ScenarioOutcome(away.quote_key),
                ),
            )
        ]

    def _authority(self):
        assessment = assess_betfair_historical_market_definition_authority(
            market_id="match_odds",
            market_definition={
                "eventId": "event-1",
                "eventTypeId": "2593174",
                "marketType": "MATCH_ODDS",
                "status": "OPEN",
                "runners": [{"id": "away"}, {"id": "home"}],
            },
            provider_publish_at="2026-09-18T15:00:00Z",
            observed_at="2026-09-18T15:00:01Z",
        )
        self.assertEqual(
            assessment.status,
            OutcomeAuthorityStatus.PROVEN_EXHAUSTIVE,
        )
        self.assertIsNotNone(assessment.authority)
        return assessment.authority

    def test_exact_search_is_stable_if_source_ticket_settles_after_snapshot(self) -> None:
        book = PaperBook("100")
        home = TicketLeg("event-1", "winner", "home", Decimal("2"))
        away = TicketLeg("event-1", "winner", "away", Decimal("2"))
        ticket = book.open_ticket([home], "10")
        engine = ScenarioSearchEngine()
        original_enumerate = ScenarioSearchEngine._enumerate

        def settle_then_enumerate(self, tickets, groups):
            ticket.status = TicketStatus.WON
            ticket.payout = Decimal("20")
            ticket.settled_at = "2026-09-23T11:34:00+00:00"
            return original_enumerate(self, tickets, groups)

        with patch.object(
            ScenarioSearchEngine,
            "_enumerate",
            new=settle_then_enumerate,
        ):
            report = engine.analyse([ticket], self._winner_group(home, away))

        self.assertEqual(ticket.status, TicketStatus.WON)
        self.assertEqual(report.observed_worst, Decimal("-10"))
        self.assertEqual(report.observed_best, Decimal("10"))
        self.assertTrue(report.worst_proven)
        self.assertTrue(report.best_proven)

    def test_authoritative_search_keeps_provider_binding_and_frozen_economics(self) -> None:
        authority = self._authority()
        self.assertIsNotNone(authority)

        book = PaperBook("100")
        home = TicketLeg(
            "event-1",
            "match_odds",
            "home",
            Decimal("2"),
            sport="table_tennis",
        )
        ticket = book.open_ticket(
            [home],
            "10",
            provider_source_ids=(self.SOURCE_ID,),
        )
        original_profit = PortfolioEngine.scenario_profit_settlements
        mutated = False

        def settle_original_then_evaluate(tickets, settlement_by_quote):
            nonlocal mutated
            if not mutated:
                mutated = True
                ticket.status = TicketStatus.WON
                ticket.payout = Decimal("20")
                ticket.settled_at = "2026-09-23T11:34:00+00:00"
            return original_profit(tickets, settlement_by_quote)

        with patch.object(
            PortfolioEngine,
            "scenario_profit_settlements",
            side_effect=settle_original_then_evaluate,
        ):
            report = ScenarioSearchEngine().analyse_authoritative(
                [ticket],
                [authority],
                decision_as_of=self.DECISION_AS_OF,
            )

        self.assertTrue(mutated)
        self.assertEqual(ticket.status, TicketStatus.WON)
        self.assertEqual(report.observed_worst, Decimal("-10"))
        self.assertEqual(report.observed_best, Decimal("10"))
        self.assertTrue(report.outcome_space_exhaustive)
        self.assertFalse(report.outcome_space_exact)
        self.assertEqual(
            report.outcome_authority_sha256s,
            (authority.authority_sha256,),
        )

    def test_provider_source_mutation_during_snapshot_fails_closed(self) -> None:
        authority = self._authority()
        self.assertIsNotNone(authority)

        book = PaperBook("100")
        home = TicketLeg(
            "event-1",
            "match_odds",
            "home",
            Decimal("2"),
            sport="table_tennis",
        )
        ticket = book.open_ticket(
            [home],
            "10",
            provider_source_ids=(self.SOURCE_ID,),
        )
        canonical_ticket_type = type(ticket)
        copied = False

        def copy_then_mutate_source(*args, **kwargs):
            nonlocal copied
            snapshot = canonical_ticket_type(*args, **kwargs)
            if not copied:
                copied = True
                ticket.provider_source_ids = ("provider-b",)
            return snapshot

        with patch(
            "autosport.portfolio.PaperTicket",
            side_effect=copy_then_mutate_source,
        ):
            with self.assertRaisesRegex(
                ValueError,
                "portfolio ticket changed during snapshot",
            ):
                ScenarioSearchEngine().analyse_authoritative(
                    [ticket],
                    [authority],
                    decision_as_of=self.DECISION_AS_OF,
                )

        self.assertTrue(copied)
        self.assertEqual(ticket.provider_source_ids, ("provider-b",))


if __name__ == "__main__":
    unittest.main()
