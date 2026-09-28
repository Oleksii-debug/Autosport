from __future__ import annotations

import unittest

from autosport.market_outcomes import (
    OutcomeAuthorityStatus,
    assess_betfair_historical_market_definition_authority,
)


class BetfairMarketDefinitionProviderOriginAuthorityTests(unittest.TestCase):
    def test_caller_constructed_definition_cannot_mint_provider_outcome_authority(self) -> None:
        fabricated_definition = {
            "eventId": "caller-invented-event",
            "eventTypeId": "2593174",
            "marketType": "MATCH_ODDS",
            "status": "OPEN",
            "complete": True,
            "runners": [{"id": "101"}, {"id": "202"}],
        }

        assessment = assess_betfair_historical_market_definition_authority(
            market_id="caller-invented-market",
            market_definition=fabricated_definition,
            provider_publish_at="2026-09-22T10:00:00Z",
            observed_at="2026-09-22T10:00:01Z",
        )

        self.assertEqual(assessment.status, OutcomeAuthorityStatus.REFUSED)
        self.assertIsNone(assessment.authority)
        self.assertEqual(
            assessment.refusal_reason,
            "betfair_market_definition_provider_origin_unverified",
        )


if __name__ == "__main__":
    unittest.main()
