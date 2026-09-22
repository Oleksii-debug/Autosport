from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal

from autosport.betfair_tennis_outcomes import (
    assess_betfair_tennis_historical_market_definition_authority,
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


class BetfairTennisOutcomeAuthorityTests(unittest.TestCase):
    DECISION_AS_OF = datetime(2026, 9, 22, 10, 0, 2, tzinfo=timezone.utc)
    SOURCE_ID = "betfair_exchange_historical"

    @staticmethod
    def _definition(
        *,
        event_type_id: str = "2",
        status: str = "OPEN",
        market_type: str = "MATCH_ODDS",
        complete: object = True,
        selection_ids: tuple[object, ...] = ("player-b", "player-a"),
    ) -> dict[str, object]:
        return {
            "eventId": "same-provider-event",
            "eventTypeId": event_type_id,
            "marketType": market_type,
            "status": status,
            "complete": complete,
            "runners": [{"id": selection_id} for selection_id in selection_ids],
        }

    def _tennis(self):
        assessment = assess_betfair_tennis_historical_market_definition_authority(
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

    def test_tennis_authority_is_provider_and_sport_bound(self):
        tennis = self._tennis()
        table_tennis = self._table_tennis()

        self.assertEqual(tennis.identity.sport, "tennis")
        self.assertEqual(tennis.selection_ids, ("player-a", "player-b"))
        self.assertEqual(tennis.terminal_state_count, 9)
        self.assertFalse(tennis.terminal_space_exact)
        self.assertTrue(
            tennis.source_revision.startswith(
                "betfair-tennis-market-definition:"
            )
        )

        self.assertNotEqual(
            tennis.identity.quote_key("player-a"),
            table_tennis.identity.quote_key("player-a"),
        )
        self.assertNotEqual(
            tennis.settlement_rules_sha256,
            table_tennis.settlement_rules_sha256,
        )
        self.assertNotEqual(
            tennis.verification_protocol_sha256,
            table_tennis.verification_protocol_sha256,
        )

    def test_same_provider_local_quote_identity_is_sport_bound_and_round_trips(self):
        base = {
            "event_id": "same-provider-event",
            "market_id": "same-provider-market",
            "selection_id": "player-a",
            "decimal_odds": "2",
            "observed_ts": "2026-09-22T10:00:00Z",
            "source_id": self.SOURCE_ID,
            "sequence": 1,
            "market_type": "winner",
        }
        tennis = MarketEvent.from_dict({**base, "sport": "tennis"})
        table_tennis = MarketEvent.from_dict({**base, "sport": "table_tennis"})

        self.assertNotEqual(tennis.quote_key, table_tennis.quote_key)
        self.assertNotEqual(tennis.dedupe_key, table_tennis.dedupe_key)
        self.assertEqual(MarketEvent.from_dict(tennis.to_dict()), tennis)
        self.assertEqual(MarketEvent.from_dict(table_tennis.to_dict()), table_tennis)

    def test_tennis_terminal_cover_preserves_each_runner_as_a_selection(self):
        tennis = self._tennis()
        player_a_wins = next(
            state
            for state in tennis.terminal_states
            if dict(state.settlements)
            == {
                "player-a": SettlementResult.WIN,
                "player-b": SettlementResult.LOSS,
            }
        )

        settlement = tennis.settlement_by_quote(player_a_wins)

        self.assertEqual(
            settlement[tennis.identity.quote_key("player-a")],
            "win",
        )
        self.assertEqual(
            settlement[tennis.identity.quote_key("player-b")],
            "loss",
        )

    def test_tennis_and_table_tennis_share_generic_scenario_engine_without_alias(self):
        tennis = self._tennis()
        table_tennis = self._table_tennis()
        book = PaperBook("100")
        tennis_ticket = book.open_ticket(
            [
                TicketLeg(
                    "same-provider-event",
                    "same-provider-market",
                    "player-a",
                    Decimal("2"),
                    sport="tennis",
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
                    "player-a",
                    Decimal("2"),
                    sport="table_tennis",
                )
            ],
            "10",
            provider_source_ids=(self.SOURCE_ID,),
        )

        report = ScenarioSearchEngine().analyse_authoritative(
            [tennis_ticket, table_tennis_ticket],
            [tennis, table_tennis],
            decision_as_of=self.DECISION_AS_OF,
        )

        self.assertEqual(report.mode, "authoritative-conservative-enumeration")
        self.assertTrue(report.outcome_space_exhaustive)
        self.assertFalse(report.outcome_space_exact)
        self.assertEqual(report.total_states, 9 * 9)
        self.assertEqual(
            set(report.outcome_authority_sha256s),
            {tennis.authority_sha256, table_tennis.authority_sha256},
        )

    def test_wrong_sport_market_or_status_fail_closed(self):
        wrong_sport = (
            assess_betfair_tennis_historical_market_definition_authority(
                market_id="m-1",
                market_definition=self._definition(event_type_id="1"),
                provider_publish_at="2026-09-22T10:00:00Z",
                observed_at="2026-09-22T10:00:01Z",
            )
        )
        self.assertEqual(wrong_sport.status, OutcomeAuthorityStatus.REFUSED)
        self.assertEqual(
            wrong_sport.refusal_reason,
            "betfair_event_type_has_no_verified_tennis_roster_protocol",
        )

        wrong_market = (
            assess_betfair_tennis_historical_market_definition_authority(
                market_id="m-2",
                market_definition=self._definition(market_type="SET_WINNER"),
                provider_publish_at="2026-09-22T10:00:00Z",
                observed_at="2026-09-22T10:00:01Z",
            )
        )
        self.assertEqual(wrong_market.status, OutcomeAuthorityStatus.REFUSED)
        self.assertEqual(
            wrong_market.refusal_reason,
            "market_type_has_no_supported_terminal_settlement_semantics",
        )

        closed = assess_betfair_tennis_historical_market_definition_authority(
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
            assess_betfair_tennis_historical_market_definition_authority(
                market_id="m-small",
                market_definition=self._definition(
                    selection_ids=("only-runner",)
                ),
                provider_publish_at="2026-09-22T10:00:00Z",
                observed_at="2026-09-22T10:00:01Z",
            )
        )
        self.assertEqual(too_small.status, OutcomeAuthorityStatus.REFUSED)
        self.assertEqual(
            too_small.refusal_reason,
            "betfair_tennis_match_odds_requires_exact_two_runner_roster",
        )

        too_large = (
            assess_betfair_tennis_historical_market_definition_authority(
                market_id="m-large",
                market_definition=self._definition(
                    selection_ids=("player-a", "player-b", "player-c")
                ),
                provider_publish_at="2026-09-22T10:00:00Z",
                observed_at="2026-09-22T10:00:01Z",
            )
        )
        self.assertEqual(too_large.status, OutcomeAuthorityStatus.REFUSED)
        self.assertEqual(
            too_large.refusal_reason,
            "betfair_tennis_match_odds_requires_exact_two_runner_roster",
        )

        with self.assertRaisesRegex(ValueError, "duplicate selection id"):
            assess_betfair_tennis_historical_market_definition_authority(
                market_id="m-duplicate",
                market_definition=self._definition(
                    selection_ids=("same", "same")
                ),
                provider_publish_at="2026-09-22T10:00:00Z",
                observed_at="2026-09-22T10:00:01Z",
            )

        malformed = self._definition()
        malformed["runners"] = [{"id": "ok"}, {}]
        with self.assertRaisesRegex(ValueError, "requires id"):
            assess_betfair_tennis_historical_market_definition_authority(
                market_id="m-malformed",
                market_definition=malformed,
                provider_publish_at="2026-09-22T10:00:00Z",
                observed_at="2026-09-22T10:00:01Z",
            )

    def test_tennis_requires_explicit_complete_provider_roster(self):
        incomplete = assess_betfair_tennis_historical_market_definition_authority(
            market_id="m-incomplete",
            market_definition=self._definition(complete=False),
            provider_publish_at="2026-09-22T10:00:00Z",
            observed_at="2026-09-22T10:00:01Z",
        )
        self.assertEqual(incomplete.status, OutcomeAuthorityStatus.REFUSED)
        self.assertIsNone(incomplete.authority)
        self.assertEqual(
            incomplete.refusal_reason,
            "betfair_market_definition_runner_roster_is_not_complete",
        )

        for malformed in (None, 0, 1, "true"):
            with self.subTest(complete=malformed):
                with self.assertRaisesRegex(
                    ValueError,
                    "marketDefinition.complete must be a boolean",
                ):
                    assess_betfair_tennis_historical_market_definition_authority(
                        market_id="m-malformed-complete",
                        market_definition=self._definition(complete=malformed),
                        provider_publish_at="2026-09-22T10:00:00Z",
                        observed_at="2026-09-22T10:00:01Z",
                    )

        missing = self._definition()
        del missing["complete"]
        with self.assertRaisesRegex(
            ValueError,
            "marketDefinition.complete must be a boolean",
        ):
            assess_betfair_tennis_historical_market_definition_authority(
                market_id="m-missing-complete",
                market_definition=missing,
                provider_publish_at="2026-09-22T10:00:00Z",
                observed_at="2026-09-22T10:00:01Z",
            )

    def test_runner_id_wire_type_fails_closed_before_string_coercion(self):
        for invalid_id in (True, ["player-a"], {"nested": "player-a"}):
            malformed = self._definition()
            malformed["runners"] = [{"id": "valid"}, {"id": invalid_id}]
            with self.subTest(invalid_id=invalid_id):
                with self.assertRaisesRegex(
                    ValueError,
                    "id must be string or integer",
                ):
                    assess_betfair_tennis_historical_market_definition_authority(
                        market_id="m-runner-type",
                        market_definition=malformed,
                        provider_publish_at="2026-09-22T10:00:00Z",
                        observed_at="2026-09-22T10:00:01Z",
                    )

        mixed_scalar_ids = self._definition(selection_ids=(12, "player-a"))
        assessment = assess_betfair_tennis_historical_market_definition_authority(
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
        self.assertEqual(assessment.authority.selection_ids, ("12", "player-a"))

    def test_provider_publish_time_cannot_be_backdated(self):
        with self.assertRaisesRegex(
            ValueError,
            "provider_publish_at must not be after observed_at",
        ):
            assess_betfair_tennis_historical_market_definition_authority(
                market_id="m-time",
                market_definition=self._definition(),
                provider_publish_at="2026-09-22T10:00:02Z",
                observed_at="2026-09-22T10:00:01Z",
            )

    def test_durable_readback_requires_fresh_same_sport_source_reverification(self):
        tennis = self._tennis()
        raw = tennis.to_dict()

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
            verified_authority=self._tennis(),
        )
        self.assertEqual(restored, tennis)
        self.assertEqual(restored.authority_sha256, tennis.authority_sha256)


if __name__ == "__main__":
    unittest.main()
