from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal

from autosport.betfair_volleyball_outcomes import (
    assess_betfair_volleyball_historical_market_definition_authority,
)
from autosport.domain import TicketLeg
from autosport.market_outcomes import (
    MarketSettlementOutcomeAuthority,
    OutcomeAuthorityStatus,
    assess_betfair_historical_market_definition_authority,
)
from autosport.paper import PaperBook
from autosport.scenario_search import ScenarioSearchEngine


class BetfairVolleyballOutcomeAuthorityTests(unittest.TestCase):
    DECISION_AS_OF = datetime(2026, 9, 22, 14, 0, 2, tzinfo=timezone.utc)
    SOURCE_ID = "betfair_exchange_historical"

    @staticmethod
    def _definition(
        *,
        event_type_id: str = "998917",
        status: str = "OPEN",
        market_type: str = "MATCH_ODDS",
        selection_ids: tuple[object, ...] = (202, 101),
    ) -> dict[str, object]:
        return {
            "eventId": "same-provider-event",
            "eventTypeId": event_type_id,
            "marketType": market_type,
            "status": status,
            "runners": [{"id": selection_id} for selection_id in selection_ids],
        }

    def _volleyball(self):
        assessment = assess_betfair_volleyball_historical_market_definition_authority(
            market_id="same-provider-market",
            market_definition=self._definition(),
            provider_publish_at="2026-09-22T14:00:00Z",
            observed_at="2026-09-22T14:00:01Z",
        )
        self.assertEqual(
            assessment.status,
            OutcomeAuthorityStatus.PROVEN_EXHAUSTIVE,
        )
        self.assertIsNotNone(assessment.authority)
        return assessment.authority

    def _table_tennis(self):
        assessment = assess_betfair_historical_market_definition_authority(
            market_id="same-provider-market",
            market_definition=self._definition(event_type_id="2593174"),
            provider_publish_at="2026-09-22T14:00:00Z",
            observed_at="2026-09-22T14:00:01Z",
        )
        self.assertEqual(
            assessment.status,
            OutcomeAuthorityStatus.PROVEN_EXHAUSTIVE,
        )
        self.assertIsNotNone(assessment.authority)
        return assessment.authority

    def test_volleyball_authority_is_provider_sport_and_protocol_bound(self):
        volleyball = self._volleyball()
        table_tennis = self._table_tennis()

        self.assertEqual(volleyball.identity.sport, "volleyball")
        self.assertEqual(volleyball.selection_ids, ("101", "202"))
        self.assertEqual(volleyball.terminal_state_count, 9)
        self.assertFalse(volleyball.terminal_space_exact)
        self.assertTrue(
            volleyball.source_revision.startswith(
                "betfair-volleyball-market-definition:"
            )
        )
        self.assertNotEqual(
            volleyball.identity.quote_key("101"),
            table_tennis.identity.quote_key("101"),
        )
        self.assertNotEqual(
            volleyball.settlement_rules_sha256,
            table_tennis.settlement_rules_sha256,
        )
        self.assertNotEqual(
            volleyball.verification_protocol_sha256,
            table_tennis.verification_protocol_sha256,
        )

    def test_volleyball_and_table_tennis_share_generic_scenario_engine_without_alias(self):
        volleyball = self._volleyball()
        table_tennis = self._table_tennis()
        book = PaperBook("100")
        volleyball_ticket = book.open_ticket(
            [
                TicketLeg(
                    "same-provider-event",
                    "same-provider-market",
                    "101",
                    Decimal("2"),
                    sport="volleyball",
                )
            ],
            "10",
            provider_source_ids=(self.SOURCE_ID,),
        )
        table_tennis_ticket = book.open_ticket(
            [
                TicketLeg(
                    "same-provider-event",
                    "same-provider-market",
                    "101",
                    Decimal("2"),
                    sport="table_tennis",
                )
            ],
            "10",
            provider_source_ids=(self.SOURCE_ID,),
        )

        report = ScenarioSearchEngine().analyse_authoritative(
            [volleyball_ticket, table_tennis_ticket],
            [volleyball, table_tennis],
            decision_as_of=self.DECISION_AS_OF,
        )

        self.assertEqual(report.mode, "authoritative-conservative-enumeration")
        self.assertTrue(report.outcome_space_exhaustive)
        self.assertFalse(report.outcome_space_exact)
        self.assertEqual(report.total_states, 9 * 9)
        self.assertEqual(
            set(report.outcome_authority_sha256s),
            {volleyball.authority_sha256, table_tennis.authority_sha256},
        )

    def test_volleyball_authority_restart_requires_source_reverification(self):
        volleyball = self._volleyball()
        raw = volleyball.to_dict()

        with self.assertRaisesRegex(
            ValueError,
            "requires separately verified source authority",
        ):
            MarketSettlementOutcomeAuthority.from_dict(raw)

        reverified = self._volleyball()
        restored = MarketSettlementOutcomeAuthority.from_dict(
            raw,
            verified_authority=reverified,
        )
        self.assertEqual(restored, volleyball)
        self.assertEqual(restored.authority_sha256, volleyball.authority_sha256)

    def test_wrong_sport_market_family_and_non_open_status_fail_closed(self):
        wrong_sport = assess_betfair_volleyball_historical_market_definition_authority(
            market_id="same-provider-market",
            market_definition=self._definition(event_type_id="2593174"),
            provider_publish_at="2026-09-22T14:00:00Z",
            observed_at="2026-09-22T14:00:01Z",
        )
        self.assertEqual(wrong_sport.status, OutcomeAuthorityStatus.REFUSED)
        self.assertEqual(
            wrong_sport.refusal_reason,
            "betfair_event_type_has_no_verified_volleyball_roster_protocol",
        )

        wrong_market = assess_betfair_volleyball_historical_market_definition_authority(
            market_id="same-provider-market",
            market_definition=self._definition(market_type="TOTAL_POINTS"),
            provider_publish_at="2026-09-22T14:00:00Z",
            observed_at="2026-09-22T14:00:01Z",
        )
        self.assertEqual(wrong_market.status, OutcomeAuthorityStatus.REFUSED)
        self.assertEqual(
            wrong_market.refusal_reason,
            "market_type_has_no_supported_terminal_settlement_semantics",
        )

        closed = assess_betfair_volleyball_historical_market_definition_authority(
            market_id="same-provider-market",
            market_definition=self._definition(status="CLOSED"),
            provider_publish_at="2026-09-22T14:00:00Z",
            observed_at="2026-09-22T14:00:01Z",
        )
        self.assertEqual(closed.status, OutcomeAuthorityStatus.REFUSED)
        self.assertEqual(
            closed.refusal_reason,
            "betfair_market_definition_is_not_open_at_roster_revision",
        )

    def test_missing_or_too_small_roster_fails_closed(self):
        definition = self._definition()
        definition["runners"] = [{"id": 101}]
        assessment = assess_betfair_volleyball_historical_market_definition_authority(
            market_id="same-provider-market",
            market_definition=definition,
            provider_publish_at="2026-09-22T14:00:00Z",
            observed_at="2026-09-22T14:00:01Z",
        )
        self.assertEqual(assessment.status, OutcomeAuthorityStatus.REFUSED)
        self.assertEqual(
            assessment.refusal_reason,
            "betfair_market_definition_lacks_authoritative_runner_roster",
        )

    def test_runner_ids_reject_ambiguous_coercions_and_duplicates(self):
        for invalid_id in (True, 1.5, ["101"], {"id": 101}):
            with self.subTest(invalid_id=invalid_id):
                with self.assertRaisesRegex(
                    ValueError,
                    "id must be string or integer",
                ):
                    assess_betfair_volleyball_historical_market_definition_authority(
                        market_id="same-provider-market",
                        market_definition=self._definition(
                            selection_ids=(invalid_id, 202),
                        ),
                        provider_publish_at="2026-09-22T14:00:00Z",
                        observed_at="2026-09-22T14:00:01Z",
                    )

        with self.assertRaisesRegex(ValueError, "duplicate selection id"):
            assess_betfair_volleyball_historical_market_definition_authority(
                market_id="same-provider-market",
                market_definition=self._definition(selection_ids=(101, "101")),
                provider_publish_at="2026-09-22T14:00:00Z",
                observed_at="2026-09-22T14:00:01Z",
            )

    def test_provider_publish_time_cannot_arrive_after_observation(self):
        with self.assertRaisesRegex(
            ValueError,
            "provider_publish_at must not be after observed_at",
        ):
            assess_betfair_volleyball_historical_market_definition_authority(
                market_id="same-provider-market",
                market_definition=self._definition(),
                provider_publish_at="2026-09-22T14:00:02Z",
                observed_at="2026-09-22T14:00:01Z",
            )


if __name__ == "__main__":
    unittest.main()
