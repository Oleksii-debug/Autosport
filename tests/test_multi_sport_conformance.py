from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal

from autosport.betfair_soccer_outcomes import (
    assess_betfair_soccer_historical_market_definition_authority,
)
from autosport.domain import TicketLeg
from autosport.market_outcomes import (
    MarketSettlementOutcomeAuthority,
    OutcomeAuthorityStatus,
    SettlementResult,
    assess_betfair_historical_market_definition_authority,
)
from autosport.paper import PaperBook
from autosport.scenario_search import ScenarioSearchEngine


class MultiSportConformanceTests(unittest.TestCase):
    SOURCE_ID = "betfair_exchange_historical"
    DECISION_AS_OF = datetime(2026, 9, 18, 15, 0, 2, tzinfo=timezone.utc)

    @staticmethod
    def _definition(
        *,
        event_type_id: str,
        selection_ids: tuple[str, ...],
        event_id: str = "same-event",
        status: str = "OPEN",
        market_type: str = "MATCH_ODDS",
    ) -> dict[str, object]:
        return {
            "eventId": event_id,
            "eventTypeId": event_type_id,
            "marketType": market_type,
            "status": status,
            "runners": [{"id": selection_id} for selection_id in selection_ids],
        }

    def _table_tennis(self, selection_ids: tuple[str, ...] = ("away", "home")):
        assessment = assess_betfair_historical_market_definition_authority(
            market_id="same-market",
            market_definition=self._definition(
                event_type_id="2593174",
                selection_ids=selection_ids,
            ),
            provider_publish_at="2026-09-18T15:00:00Z",
            observed_at="2026-09-18T15:00:01Z",
        )
        self.assertEqual(assessment.status, OutcomeAuthorityStatus.PROVEN_EXHAUSTIVE)
        self.assertIsNotNone(assessment.authority)
        return assessment.authority

    def _soccer(self, selection_ids: tuple[str, ...] = ("away", "draw", "home")):
        assessment = assess_betfair_soccer_historical_market_definition_authority(
            market_id="same-market",
            market_definition=self._definition(
                event_type_id="1",
                selection_ids=selection_ids,
            ),
            provider_publish_at="2026-09-18T15:00:00Z",
            observed_at="2026-09-18T15:00:01Z",
        )
        self.assertEqual(assessment.status, OutcomeAuthorityStatus.PROVEN_EXHAUSTIVE)
        self.assertIsNotNone(assessment.authority)
        return assessment.authority

    def test_second_sport_is_sport_bound_and_three_way_without_cross_sport_alias(self):
        table_tennis = self._table_tennis()
        soccer = self._soccer()

        self.assertEqual(table_tennis.identity.sport, "table_tennis")
        self.assertEqual(soccer.identity.sport, "soccer")
        self.assertEqual(table_tennis.selection_ids, ("away", "home"))
        self.assertEqual(soccer.selection_ids, ("away", "draw", "home"))
        self.assertEqual(table_tennis.terminal_state_count, 9)
        self.assertEqual(soccer.terminal_state_count, 27)
        self.assertFalse(table_tennis.terminal_space_exact)
        self.assertFalse(soccer.terminal_space_exact)

        # The same provider-local event/market/selection identifiers remain disjoint.
        self.assertNotEqual(
            table_tennis.identity.quote_key("home"),
            soccer.identity.quote_key("home"),
        )
        self.assertNotEqual(
            table_tennis.verification_protocol_sha256,
            soccer.verification_protocol_sha256,
        )
        self.assertNotEqual(
            table_tennis.settlement_rules_sha256,
            soccer.settlement_rules_sha256,
        )

    def test_soccer_draw_is_a_first_class_terminal_selection_not_win_loss_coercion(self):
        soccer = self._soccer()
        draw_wins = next(
            state
            for state in soccer.terminal_states
            if dict(state.settlements)
            == {
                "away": SettlementResult.LOSS,
                "draw": SettlementResult.WIN,
                "home": SettlementResult.LOSS,
            }
        )
        settlement = soccer.settlement_by_quote(draw_wins)
        self.assertEqual(settlement[soccer.identity.quote_key("draw")], "win")
        self.assertEqual(settlement[soccer.identity.quote_key("home")], "loss")
        self.assertEqual(settlement[soccer.identity.quote_key("away")], "loss")

    def test_two_sports_share_generic_scenario_engine_without_identity_collision(self):
        table_tennis = self._table_tennis()
        soccer = self._soccer()
        book = PaperBook("100")
        tt_home = TicketLeg(
            "same-event",
            "same-market",
            "home",
            Decimal("2"),
            sport="table_tennis",
        )
        soccer_home = TicketLeg(
            "same-event",
            "same-market",
            "home",
            Decimal("2"),
            sport="soccer",
        )
        tt_ticket = book.open_ticket(
            [tt_home], "10", provider_source_ids=(self.SOURCE_ID,)
        )
        soccer_ticket = book.open_ticket(
            [soccer_home], "10", provider_source_ids=(self.SOURCE_ID,)
        )

        report = ScenarioSearchEngine().analyse_authoritative(
            [tt_ticket, soccer_ticket],
            [table_tennis, soccer],
            decision_as_of=self.DECISION_AS_OF,
        )

        self.assertEqual(report.mode, "authoritative-conservative-enumeration")
        self.assertTrue(report.outcome_space_exhaustive)
        self.assertFalse(report.outcome_space_exact)
        self.assertEqual(report.total_states, 9 * 27)
        self.assertEqual(
            set(report.outcome_authority_sha256s),
            {table_tennis.authority_sha256, soccer.authority_sha256},
        )

    def test_soccer_authority_restart_requires_source_reverification(self):
        soccer = self._soccer()
        raw = soccer.to_dict()

        with self.assertRaisesRegex(
            ValueError,
            "requires separately verified source authority",
        ):
            MarketSettlementOutcomeAuthority.from_dict(raw)

        reverified = self._soccer()
        restored = MarketSettlementOutcomeAuthority.from_dict(
            raw,
            verified_authority=reverified,
        )
        self.assertEqual(restored, soccer)
        self.assertEqual(restored.authority_sha256, soccer.authority_sha256)

    def test_soccer_adapter_fails_closed_for_wrong_event_type_and_market_family(self):
        wrong_sport = assess_betfair_soccer_historical_market_definition_authority(
            market_id="same-market",
            market_definition=self._definition(
                event_type_id="2593174",
                selection_ids=("away", "home"),
            ),
            provider_publish_at="2026-09-18T15:00:00Z",
            observed_at="2026-09-18T15:00:01Z",
        )
        self.assertEqual(wrong_sport.status, OutcomeAuthorityStatus.REFUSED)
        self.assertEqual(wrong_sport.identity.sport, "soccer")
        self.assertEqual(
            wrong_sport.refusal_reason,
            "betfair_event_type_has_no_verified_soccer_roster_protocol",
        )

        wrong_market = assess_betfair_soccer_historical_market_definition_authority(
            market_id="total-goals",
            market_definition=self._definition(
                event_type_id="1",
                selection_ids=("over", "under"),
                market_type="OVER_UNDER_25",
            ),
            provider_publish_at="2026-09-18T15:00:00Z",
            observed_at="2026-09-18T15:00:01Z",
        )
        self.assertEqual(wrong_market.status, OutcomeAuthorityStatus.REFUSED)
        self.assertEqual(
            wrong_market.refusal_reason,
            "market_type_has_no_supported_terminal_settlement_semantics",
        )


if __name__ == "__main__":
    unittest.main()
