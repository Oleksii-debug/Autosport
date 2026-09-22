from __future__ import annotations

import unittest

from autosport.market_outcomes import (
    OutcomeAuthorityStatus,
    assess_betfair_historical_market_definition_authority,
)


class BetfairMarketDefinitionRunnerIdTypeIntegrityTests(unittest.TestCase):
    @staticmethod
    def _definition(runner_ids: tuple[object, ...]) -> dict[str, object]:
        return {
            "eventId": "event-1",
            "eventTypeId": "2593174",
            "marketType": "MATCH_ODDS",
            "status": "OPEN",
            "runners": [{"id": runner_id} for runner_id in runner_ids],
        }

    def test_valid_canonical_string_runner_ids_remain_supported(self) -> None:
        assessment = assess_betfair_historical_market_definition_authority(
            market_id="match-odds",
            market_definition=self._definition(("away", "home")),
            provider_publish_at="2026-09-18T15:00:00Z",
            observed_at="2026-09-18T15:00:01Z",
        )

        self.assertEqual(assessment.status, OutcomeAuthorityStatus.PROVEN_EXHAUSTIVE)
        self.assertIsNotNone(assessment.authority)
        assert assessment.authority is not None
        self.assertEqual(assessment.authority.selection_ids, ("away", "home"))

    def test_non_scalar_runner_ids_fail_before_identity_coercion(self) -> None:
        malformed_ids: tuple[object, ...] = (
            True,
            {"nested": "selection"},
            ["selection"],
        )

        for malformed in malformed_ids:
            with self.subTest(runner_id=malformed):
                with self.assertRaises((TypeError, ValueError)):
                    assess_betfair_historical_market_definition_authority(
                        market_id="match-odds",
                        market_definition=self._definition(("away", malformed)),
                        provider_publish_at="2026-09-18T15:00:00Z",
                        observed_at="2026-09-18T15:00:01Z",
                    )


if __name__ == "__main__":
    unittest.main()
