from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal

from autosport.betfair_basketball_outcomes import (
    assess_betfair_basketball_historical_market_definition_authority,
)
from autosport.domain import MarketEvent, TicketLeg
from autosport.market_outcomes import (
    MarketSettlementOutcomeAuthority,
    OutcomeAuthorityStatus,
    SettlementResult,
    assess_betfair_historical_market_definition_authority,
)
from autosport.paper import PaperBook
from autosport.scenario_search import ScenarioSearchEngine


class BetfairBasketballOutcomeAuthorityTests(unittest.TestCase):
    DECISION_AS_OF = datetime(2026, 9, 22, 10, 0, 2, tzinfo=timezone.utc)
    SOURCE_ID = "betfair_exchange_historical"

    @staticmethod
    def _definition(
        *,
        event_type_id: str = "7522",
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

    def _basketball(self):
        assessment = assess_betfair_basketball_historical_market_definition_authority(
            market_id="same-provider-market",
            market_definition=self._definition(),
            provider_publish_at="2026-09-22T10:00:00Z",
            observed_at="2026-09-22T10:00:01Z",
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
            provider_publish_at="2026-09-22T10:00:00Z",
            observed_at="2026-09-22T10:00:01Z",
        )
        self.assertEqual(
            assessment.status,
            OutcomeAuthorityStatus.PROVEN_EXHAUSTIVE,
        )
        self.assertIsNotNone(assessment.authority)
        return assessment.authority

    def test_basketball_authority_is_provider_and_sport_bound(self):
        basketball = self._basketball()
        table_tennis = self._table_tennis()

        self.assertEqual(basketball.identity.sport, "basketball")
        self.assertEqual(basketball.selection_ids, ("101", "202"))
        self.assertEqual(basketball.terminal_state_count, 9)
        self.assertFalse(basketball.terminal_space_exact)
        self.assertTrue(
            basketball.source_revision.startswith(
                "betfair-basketball-market-definition:"
            )
        )

        self.assertNotEqual(
            basketball.identity.quote_key("101"),
            table_tennis.identity.quote_key("101"),
        )
        self.assertNotEqual(
            basketball.settlement_rules_sha256,
            table_tennis.settlement_rules_sha256,
        )
        self.assertNotEqual(
            basketball.verification_protocol_sha256,
            table_tennis.verification_protocol_sha256,
        )

    def test_same_provider_local_quote_identity_is_sport_bound_and_round_trips(self):
        base = {
            "event_id": "same-provider-event",
            "market_id": "same-provider-market",
            "selection_id": "101",
            "decimal_odds": "2",
            "observed_ts": "2026-09-22T10:00:00Z",
            "source_id": self.SOURCE_ID,
            "sequence": 1,
            "market_type": "winner",
        }
        basketball = MarketEvent.from_dict({**base, "sport": "basketball"})
        table_tennis = MarketEvent.from_dict({**base, "sport": "table_tennis"})

        self.assertNotEqual(basketball.quote_key, table_tennis.quote_key)
        self.assertNotEqual(basketball.dedupe_key, table_tennis.dedupe_key)
        self.assertEqual(MarketEvent.from_dict(basketball.to_dict()), basketball)
        self.assertEqual(MarketEvent.from_dict(table_tennis.to_dict()), table_tennis)

    def test_basketball_terminal_cover_preserves_each_runner_as_a_selection(self):
        basketball = self._basketball()
        home_wins = next(
            state
            for state in basketball.terminal_states
            if dict(state.settlements)
            == {
                "101": SettlementResult.WIN,
                "202": SettlementResult.LOSS,
            }
        )

        settlement = basketball.settlement_by_quote(home_wins)

        self.assertEqual(
            settlement[basketball.identity.quote_key("101")],
            "win",
        )
        self.assertEqual(
            settlement[basketball.identity.quote_key("202")],
            "loss",
        )

    def test_basketball_and_table_tennis_share_generic_scenario_engine_without_alias(self):
        basketball = self._basketball()
        table_tennis = self._table_tennis()
        book = PaperBook("100")
        basketball_ticket = book.open_ticket(
            [
                TicketLeg(
                    "same-provider-event",
                    "same-provider-market",
                    "101",
                    Decimal("2"),
                    sport="basketball",
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
            [basketball_ticket, table_tennis_ticket],
            [basketball, table_tennis],
            decision_as_of=self.DECISION_AS_OF,
        )

        self.assertEqual(report.mode, "authoritative-conservative-enumeration")
        self.assertTrue(report.outcome_space_exhaustive)
        self.assertFalse(report.outcome_space_exact)
        self.assertEqual(report.total_states, 9 * 9)
        self.assertEqual(
            set(report.outcome_authority_sha256s),
            {basketball.authority_sha256, table_tennis.authority_sha256},
        )

    def test_wrong_sport_market_or_status_fail_closed(self):
        wrong_sport = (
            assess_betfair_basketball_historical_market_definition_authority(
                market_id="m-1",
                market_definition=self._definition(event_type_id="1"),
                provider_publish_at="2026-09-22T10:00:00Z",
                observed_at="2026-09-22T10:00:01Z",
            )
        )
        self.assertEqual(wrong_sport.status, OutcomeAuthorityStatus.REFUSED)
        self.assertEqual(
            wrong_sport.refusal_reason,
            "betfair_event_type_has_no_verified_basketball_roster_protocol",
        )

        wrong_market = (
            assess_betfair_basketball_historical_market_definition_authority(
                market_id="m-2",
                market_definition=self._definition(market_type="OVER_UNDER_215_5"),
                provider_publish_at="2026-09-22T10:00:00Z",
                observed_at="2026-09-22T10:00:01Z",
            )
        )
        self.assertEqual(wrong_market.status, OutcomeAuthorityStatus.REFUSED)
        self.assertEqual(
            wrong_market.refusal_reason,
            "market_type_has_no_supported_terminal_settlement_semantics",
        )

        closed = assess_betfair_basketball_historical_market_definition_authority(
            market_id="m-3",
            market_definition=self._definition(status="CLOSED"),
            provider_publish_at="2026-09-22T10:00:00Z",
            observed_at="2026-09-22T10:00:01Z",
        )
        self.assertEqual(closed.status, OutcomeAuthorityStatus.REFUSED)
        self.assertEqual(
            closed.refusal_reason,
            "betfair_market_definition_is_not_open_at_roster_revision",
        )

    def test_malformed_or_duplicate_roster_fails_closed(self):
        too_small = (
            assess_betfair_basketball_historical_market_definition_authority(
                market_id="m-small",
                market_definition=self._definition(selection_ids=(101,)),
                provider_publish_at="2026-09-22T10:00:00Z",
                observed_at="2026-09-22T10:00:01Z",
            )
        )
        self.assertEqual(too_small.status, OutcomeAuthorityStatus.REFUSED)
        self.assertEqual(
            too_small.refusal_reason,
            "betfair_market_definition_lacks_authoritative_runner_roster",
        )

        with self.assertRaisesRegex(ValueError, "duplicate selection id"):
            assess_betfair_basketball_historical_market_definition_authority(
                market_id="m-duplicate",
                market_definition=self._definition(selection_ids=(101, "101")),
                provider_publish_at="2026-09-22T10:00:00Z",
                observed_at="2026-09-22T10:00:01Z",
            )

        malformed = self._definition()
        malformed["runners"] = [{"id": 101}, {}]
        with self.assertRaisesRegex(ValueError, "requires id"):
            assess_betfair_basketball_historical_market_definition_authority(
                market_id="m-malformed",
                market_definition=malformed,
                provider_publish_at="2026-09-22T10:00:00Z",
                observed_at="2026-09-22T10:00:01Z",
            )

    def test_runner_id_wire_type_fails_closed_before_string_coercion(self):
        for invalid_id in (True, 10.5, [101], {"nested": 101}):
            malformed = self._definition()
            malformed["runners"] = [{"id": 101}, {"id": invalid_id}]
            with self.subTest(invalid_id=invalid_id):
                with self.assertRaisesRegex(
                    ValueError,
                    "id must be string or integer",
                ):
                    assess_betfair_basketball_historical_market_definition_authority(
                        market_id="m-runner-type",
                        market_definition=malformed,
                        provider_publish_at="2026-09-22T10:00:00Z",
                        observed_at="2026-09-22T10:00:01Z",
                    )

        mixed_scalar_ids = self._definition(selection_ids=(12, "101"))
        assessment = assess_betfair_basketball_historical_market_definition_authority(
            market_id="m-scalar-runner-ids",
            market_definition=mixed_scalar_ids,
            provider_publish_at="2026-09-22T10:00:00Z",
            observed_at="2026-09-22T10:00:01Z",
        )
        self.assertEqual(
            assessment.status,
            OutcomeAuthorityStatus.PROVEN_EXHAUSTIVE,
        )
        self.assertIsNotNone(assessment.authority)
        self.assertEqual(assessment.authority.selection_ids, ("101", "12"))

    def test_provider_publish_time_cannot_be_backdated(self):
        with self.assertRaisesRegex(
            ValueError,
            "provider_publish_at must not be after observed_at",
        ):
            assess_betfair_basketball_historical_market_definition_authority(
                market_id="m-time",
                market_definition=self._definition(),
                provider_publish_at="2026-09-22T10:00:02Z",
                observed_at="2026-09-22T10:00:01Z",
            )

    def test_durable_readback_requires_fresh_same_sport_source_reverification(self):
        basketball = self._basketball()
        raw = basketball.to_dict()

        with self.assertRaisesRegex(
            ValueError,
            "requires separately verified source authority",
        ):
            MarketSettlementOutcomeAuthority.from_dict(raw)

        table_tennis = self._table_tennis()
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            MarketSettlementOutcomeAuthority.from_dict(
                raw,
                verified_authority=table_tennis,
            )

        restored = MarketSettlementOutcomeAuthority.from_dict(
            raw,
            verified_authority=self._basketball(),
        )
        self.assertEqual(restored, basketball)
        self.assertEqual(restored.authority_sha256, basketball.authority_sha256)


if __name__ == "__main__":
    unittest.main()
