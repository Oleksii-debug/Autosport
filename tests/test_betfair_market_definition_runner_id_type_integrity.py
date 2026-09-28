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
            "complete": True,
            "runners": [{"id": runner_id} for runner_id in runner_ids],
        }

    def _assess(self, runner_ids: tuple[object, ...]):
        return assess_betfair_historical_market_definition_authority(
            market_id="match-odds",
            market_definition=self._definition(runner_ids),
            provider_publish_at="2026-09-18T15:00:00Z",
            observed_at="2026-09-18T15:00:01Z",
        )

    def test_valid_canonical_string_runner_ids_reach_origin_fence(self) -> None:
        assessment = self._assess(("away", "home"))
        self.assertEqual(assessment.status, OutcomeAuthorityStatus.REFUSED)
        self.assertIsNone(assessment.authority)
        self.assertEqual(
            assessment.refusal_reason,
            "betfair_market_definition_provider_origin_unverified",
        )

    def test_exact_positive_signed_64_bit_integer_ids_reach_origin_fence(self) -> None:
        assessment = self._assess((17, 9223372036854775807))
        self.assertEqual(assessment.status, OutcomeAuthorityStatus.REFUSED)
        self.assertIsNone(assessment.authority)
        self.assertEqual(
            assessment.refusal_reason,
            "betfair_market_definition_provider_origin_unverified",
        )

    def test_noncanonical_wire_types_fail_before_identity_coercion(self) -> None:
        malformed_ids: tuple[object, ...] = (
            True,
            1.0,
            {"nested": "selection"},
            ["selection"],
            ("selection",),
            object(),
        )
        for malformed in malformed_ids:
            with self.subTest(runner_id=repr(malformed)):
                with self.assertRaises((TypeError, ValueError)):
                    self._assess(("away", malformed))

    def test_out_of_range_integer_ids_fail_closed(self) -> None:
        for malformed in (0, -1, 9223372036854775808):
            with self.subTest(runner_id=malformed):
                with self.assertRaises(ValueError):
                    self._assess(("away", malformed))

    def test_string_integer_alias_cannot_create_duplicate_selection_identity(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate selection id"):
            self._assess((1, "1"))


if __name__ == "__main__":
    unittest.main()
