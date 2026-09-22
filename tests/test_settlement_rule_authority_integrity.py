from __future__ import annotations

import unittest
from dataclasses import replace

from autosport.market_outcomes import (
    OutcomeAuthorityStatus,
    SettlementSemantics,
    assess_betfair_historical_market_definition_authority,
)


class SettlementRuleAuthorityIntegrityTests(unittest.TestCase):
    def _conservative_authority(self):
        assessment = assess_betfair_historical_market_definition_authority(
            market_id="1.234567890",
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
        self.assertEqual(assessment.status, OutcomeAuthorityStatus.PROVEN_EXHAUSTIVE)
        self.assertIsNotNone(assessment.authority)
        return assessment.authority

    def test_standard_dataclass_replace_cannot_reissue_verified_rule_authority(self):
        authority = self._conservative_authority()
        self.assertFalse(authority.terminal_space_exact)
        self.assertFalse(hasattr(authority, "_verification_token"))

        with self.assertRaises(TypeError):
            replace(
                authority,
                settlement_semantics=SettlementSemantics.EXCLUSIVE_SINGLE_WINNER,
            )

        self.assertFalse(authority.terminal_space_exact)
        self.assertEqual(
            authority.settlement_semantics,
            SettlementSemantics.CANONICAL_WIN_LOSS_VOID_SUPERSET,
        )

    def test_caller_supplied_replacement_token_cannot_mint_exact_rule_authority(self):
        authority = self._conservative_authority()

        with self.assertRaisesRegex(
            TypeError,
            "must come from verified evidence",
        ):
            replace(
                authority,
                settlement_semantics=SettlementSemantics.EXCLUSIVE_SINGLE_WINNER,
                _verification_token=object(),
            )


if __name__ == "__main__":
    unittest.main()
