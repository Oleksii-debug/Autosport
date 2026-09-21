from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal

from autosport.domain import TicketLeg
from autosport.market_outcomes import (
    MarketSettlementOutcomeAuthority,
    OutcomeAuthorityStatus,
    SettlementResult,
    assess_betfair_historical_market_definition_authority,
)
from autosport.paper import PaperBook
from autosport.portfolio import PortfolioEngine
from autosport.scenario_search import ScenarioSearchEngine
from autosport.second_sport_conformance import (
    TEST_ONLY_SECOND_SPORT_RULE_ID,
    TEST_ONLY_SECOND_SPORT_SOURCE_ID,
    assess_test_only_second_sport_market_authority,
)


class SecondSportConformanceTests(unittest.TestCase):
    DECISION_AS_OF = datetime(2026, 9, 18, 15, 0, 2, tzinfo=timezone.utc)

    @staticmethod
    def _table_tennis_authority() -> MarketSettlementOutcomeAuthority:
        assessment = assess_betfair_historical_market_definition_authority(
            market_id="match_result",
            market_definition={
                "eventId": "same-event",
                "eventTypeId": "2593174",
                "marketType": "MATCH_ODDS",
                "status": "OPEN",
                "runners": [{"id": "away"}, {"id": "home"}],
            },
            provider_publish_at="2026-09-18T15:00:00Z",
            observed_at="2026-09-18T15:00:01Z",
        )
        assert assessment.authority is not None
        return assessment.authority

    @staticmethod
    def _second_sport_authority() -> MarketSettlementOutcomeAuthority:
        assessment = assess_test_only_second_sport_market_authority(
            event_id="same-event",
            market_id="match_result",
            market_rule_id=TEST_ONLY_SECOND_SPORT_RULE_ID,
            causal_cutoff="2026-09-18T15:00:00Z",
            observed_at="2026-09-18T15:00:01Z",
        )
        assert assessment.authority is not None
        return assessment.authority

    def test_same_display_ids_remain_sport_bound_and_semantics_are_materially_different(self):
        table_tennis = self._table_tennis_authority()
        soccer = self._second_sport_authority()

        self.assertEqual(table_tennis.identity.sport, "table_tennis")
        self.assertEqual(soccer.identity.sport, "soccer")
        self.assertNotEqual(
            table_tennis.identity.quote_key("home"),
            soccer.identity.quote_key("home"),
        )
        self.assertNotEqual(table_tennis.authority_sha256, soccer.authority_sha256)

        # Current Betfair evidence uses a conservative Cartesian cover because the
        # importer cannot prove which WINNER/LOSER/REMOVED combinations are possible.
        self.assertFalse(table_tennis.terminal_space_exact)
        self.assertEqual(table_tennis.terminal_state_count, 9)

        # The closed engineering fixture deliberately proves a different 3-way
        # semantic family: away/draw/home plus an exact all-void state.
        self.assertTrue(soccer.terminal_space_exact)
        self.assertEqual(soccer.terminal_state_count, 4)
        self.assertEqual(
            {state.state_id for state in soccer.terminal_states},
            {"winner:away", "winner:draw", "winner:home", "all_void"},
        )

    def test_test_only_evidence_identity_cannot_masquerade_as_provider_evidence(self):
        soccer = self._second_sport_authority()
        self.assertEqual(
            soccer.identity.source_id,
            "test_only_engineering_conformance",
        )
        self.assertEqual(
            soccer.identity.source_id,
            TEST_ONLY_SECOND_SPORT_SOURCE_ID,
        )
        self.assertTrue(soccer.source_revision.startswith("test-only-second-sport:"))
        self.assertNotEqual(
            soccer.identity.source_id,
            "betfair_exchange_historical",
        )

    def test_unsupported_second_sport_rule_fails_closed(self):
        assessment = assess_test_only_second_sport_market_authority(
            event_id="same-event",
            market_id="match_result",
            market_rule_id="test-only.soccer.unknown-rule.v99",
            causal_cutoff="2026-09-18T15:00:00Z",
            observed_at="2026-09-18T15:00:01Z",
        )
        self.assertEqual(assessment.status, OutcomeAuthorityStatus.REFUSED)
        self.assertIsNone(assessment.authority)
        self.assertEqual(
            assessment.refusal_reason,
            "test_only_second_sport_market_rule_unsupported",
        )

    def test_restart_requires_reverification_of_same_test_only_rule(self):
        authority = self._second_sport_authority()
        raw = authority.to_dict()

        with self.assertRaisesRegex(
            ValueError,
            "requires separately verified source authority",
        ):
            MarketSettlementOutcomeAuthority.from_dict(raw)

        reverified = self._second_sport_authority()
        restored = MarketSettlementOutcomeAuthority.from_dict(
            raw,
            verified_authority=reverified,
        )
        self.assertEqual(restored, authority)
        self.assertEqual(restored.authority_sha256, authority.authority_sha256)
        self.assertEqual(restored.identity.sport, "soccer")
        self.assertEqual(
            restored.identity.source_id,
            TEST_ONLY_SECOND_SPORT_SOURCE_ID,
        )

    def test_draw_and_all_void_preserve_exact_settlement_economics(self):
        authority = self._second_sport_authority()
        book = PaperBook("100")
        draw_leg = TicketLeg(
            "same-event",
            "match_result",
            "draw",
            Decimal("3"),
            sport="soccer",
        )
        ticket = book.open_ticket(
            [draw_leg],
            "10",
            provider_source_ids=(TEST_ONLY_SECOND_SPORT_SOURCE_ID,),
        )

        draw_state = next(
            state for state in authority.terminal_states if state.state_id == "winner:draw"
        )
        self.assertEqual(
            dict(draw_state.settlements),
            {
                "away": SettlementResult.LOSS,
                "draw": SettlementResult.WIN,
                "home": SettlementResult.LOSS,
            },
        )
        self.assertEqual(
            PortfolioEngine.scenario_profit_settlements(
                [ticket],
                authority.settlement_by_quote(draw_state),
            ),
            Decimal("20"),
        )

        void_state = next(
            state for state in authority.terminal_states if state.state_id == "all_void"
        )
        self.assertEqual(
            PortfolioEngine.scenario_profit_settlements(
                [ticket],
                authority.settlement_by_quote(void_state),
            ),
            Decimal("0"),
        )

    def test_authoritative_scenario_search_is_exact_for_second_sport_fixture(self):
        authority = self._second_sport_authority()
        book = PaperBook("100")
        ticket = book.open_ticket(
            [
                TicketLeg(
                    "same-event",
                    "match_result",
                    "draw",
                    Decimal("3"),
                    sport="soccer",
                )
            ],
            "10",
            provider_source_ids=(TEST_ONLY_SECOND_SPORT_SOURCE_ID,),
        )

        report = ScenarioSearchEngine().analyse_authoritative(
            [ticket],
            [authority],
            decision_as_of=self.DECISION_AS_OF,
        )
        self.assertEqual(report.total_states, 4)
        self.assertTrue(report.outcome_space_exhaustive)
        self.assertTrue(report.outcome_space_exact)
        self.assertTrue(report.worst_proven)
        self.assertTrue(report.best_proven)
        self.assertEqual(
            report.outcome_authority_sha256s,
            (authority.authority_sha256,),
        )

    def test_table_tennis_authority_cannot_cover_same_ids_in_second_sport(self):
        table_tennis = self._table_tennis_authority()
        book = PaperBook("100")
        soccer_ticket = book.open_ticket(
            [
                TicketLeg(
                    "same-event",
                    "match_result",
                    "home",
                    Decimal("2"),
                    sport="soccer",
                )
            ],
            "10",
            provider_source_ids=("betfair_exchange_historical",),
        )

        with self.assertRaisesRegex(
            ValueError,
            "ticket leg missing from authoritative outcome universe",
        ):
            ScenarioSearchEngine().analyse_authoritative(
                [soccer_ticket],
                [table_tennis],
                decision_as_of=self.DECISION_AS_OF,
            )


if __name__ == "__main__":
    unittest.main()
