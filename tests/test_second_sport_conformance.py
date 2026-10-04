from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal

from autosport.domain import MarketType, TicketLeg
from autosport.market_outcomes import (
    MarketOutcomeAuthorityAssessment,
    MarketOutcomeIdentity,
    MarketSettlementOutcomeAuthority,
    OutcomeAuthorityStatus,
    OutcomeRosterBasis,
    SettlementResult,
    SettlementSemantics,
    _VERIFIED_AUTHORITY_TOKEN,
    _sha256_payload,
    assess_betfair_historical_market_definition_authority,
)
from autosport.paper import PaperBook
from autosport.portfolio import PortfolioEngine
from autosport.scenario_search import ScenarioSearchEngine


# This closed second-sport fixture is deliberately test code, not shipped product
# authority.  It exercises the canonical sport-generic path without exposing a
# production API capable of issuing synthetic externally-looking authority.
TEST_ONLY_SECOND_SPORT_SOURCE_ID = "test_only_engineering_conformance"
TEST_ONLY_SECOND_SPORT_RULE_ID = "test-only.soccer.match-result-3way.v1"
_TEST_ONLY_SECOND_SPORT_SELECTION_IDS = ("away", "draw", "home")
_TEST_ONLY_SECOND_SPORT_PROTOCOL = (
    "autosport.test_only.second_sport_conformance.soccer_match_result_3way.v1"
)


def assess_test_only_second_sport_market_authority(
    *,
    event_id: str,
    market_id: str,
    market_rule_id: str,
    causal_cutoff: str,
    observed_at: str,
) -> MarketOutcomeAuthorityAssessment:
    """Issue exact authority only inside the engineering-conformance test surface."""

    identity = MarketOutcomeIdentity(
        sport="soccer",
        event_id=event_id,
        market_id=market_id,
        source_id=TEST_ONLY_SECOND_SPORT_SOURCE_ID,
        market_type=MarketType.WINNER,
    )
    if market_rule_id != TEST_ONLY_SECOND_SPORT_RULE_ID:
        return MarketOutcomeAuthorityAssessment(
            identity=identity,
            status=OutcomeAuthorityStatus.REFUSED,
            authority=None,
            refusal_reason="test_only_second_sport_market_rule_unsupported",
        )

    fixture_definition = {
        "evidence_grade": "TEST_ONLY_ENGINEERING_CONFORMANCE",
        "external_provider_evidence": False,
        "sport": identity.sport,
        "event_id": identity.event_id,
        "market_id": identity.market_id,
        "market_rule_id": TEST_ONLY_SECOND_SPORT_RULE_ID,
        "selection_ids": list(_TEST_ONLY_SECOND_SPORT_SELECTION_IDS),
        "causal_cutoff": causal_cutoff,
    }
    roster_provenance_sha256 = _sha256_payload(fixture_definition)
    settlement_protocol = {
        "evidence_grade": "TEST_ONLY_ENGINEERING_CONFORMANCE",
        "sport": identity.sport,
        "market_rule_id": TEST_ONLY_SECOND_SPORT_RULE_ID,
        "terminal_family": SettlementSemantics.EXCLUSIVE_SINGLE_WINNER_OR_ALL_VOID.value,
        "selection_ids": list(_TEST_ONLY_SECOND_SPORT_SELECTION_IDS),
        "terminal_space_exact": True,
    }
    settlement_rules_sha256 = _sha256_payload(settlement_protocol)
    verification_protocol_sha256 = _sha256_payload(
        {
            "protocol": _TEST_ONLY_SECOND_SPORT_PROTOCOL,
            "source_id": TEST_ONLY_SECOND_SPORT_SOURCE_ID,
            "external_provider_evidence": False,
            "market_rule_id": TEST_ONLY_SECOND_SPORT_RULE_ID,
            "settlement_protocol_sha256": settlement_rules_sha256,
        }
    )
    source_revision = (
        "test-only-second-sport:"
        + TEST_ONLY_SECOND_SPORT_RULE_ID
        + ":"
        + roster_provenance_sha256[:16]
    )
    authority = MarketSettlementOutcomeAuthority(
        identity=identity,
        selection_ids=_TEST_ONLY_SECOND_SPORT_SELECTION_IDS,
        roster_basis=OutcomeRosterBasis.GOVERNED_DATASET_MARKET_DEFINITION,
        settlement_semantics=SettlementSemantics.EXCLUSIVE_SINGLE_WINNER_OR_ALL_VOID,
        source_revision=source_revision,
        causal_cutoff=causal_cutoff,
        observed_at=observed_at,
        roster_provenance_sha256=roster_provenance_sha256,
        settlement_rules_sha256=settlement_rules_sha256,
        verification_protocol_sha256=verification_protocol_sha256,
        _verification_token=_VERIFIED_AUTHORITY_TOKEN,
    )
    return MarketOutcomeAuthorityAssessment(
        identity=identity,
        status=OutcomeAuthorityStatus.PROVEN_EXHAUSTIVE,
        authority=authority,
        refusal_reason=None,
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

        self.assertFalse(table_tennis.terminal_space_exact)
        self.assertEqual(table_tennis.terminal_state_count, 9)

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

    def test_authoritative_scenario_search_is_exact_for_test_fixture_only(self):
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
