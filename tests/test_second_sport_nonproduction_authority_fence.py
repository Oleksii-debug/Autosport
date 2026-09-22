from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal

from autosport.domain import TicketLeg
from autosport.market_outcomes import (
    MarketSettlementOutcomeAuthority,
    OutcomeAuthorityStatus,
)
from autosport.paper import PaperBook
from autosport.scenario_search import ScenarioSearchEngine
from autosport.second_sport_conformance import (
    TEST_ONLY_SECOND_SPORT_RULE_ID,
    TEST_ONLY_SECOND_SPORT_SOURCE_ID,
    assess_test_only_second_sport_market_authority,
)


class SecondSportNonProductionAuthorityFenceTests(unittest.TestCase):
    DECISION_AS_OF = datetime(2026, 9, 18, 15, 0, 2, tzinfo=timezone.utc)

    @staticmethod
    def _assessment():
        return assess_test_only_second_sport_market_authority(
            event_id="real-looking-event-12345",
            market_id="1.234567890",
            market_rule_id=TEST_ONLY_SECOND_SPORT_RULE_ID,
            causal_cutoff="2026-09-18T15:00:00Z",
            observed_at="2026-09-18T15:00:01Z",
        )

    @staticmethod
    def _ticket():
        book = PaperBook("100")
        return book.open_ticket(
            [
                TicketLeg(
                    "real-looking-event-12345",
                    "1.234567890",
                    "draw",
                    Decimal("3"),
                    sport="soccer",
                )
            ],
            "10",
            provider_source_ids=(TEST_ONLY_SECOND_SPORT_SOURCE_ID,),
        )

    def _assert_authority_cannot_mint_production_exact_truth(
        self,
        authority: MarketSettlementOutcomeAuthority,
    ) -> None:
        with self.assertRaises(
            ValueError,
            msg=(
                "TEST_ONLY_ENGINEERING_CONFORMANCE authority must not enter "
                "production authoritative scenario/economic truth"
            ),
        ):
            ScenarioSearchEngine().analyse_authoritative(
                [self._ticket()],
                [authority],
                decision_as_of=self.DECISION_AS_OF,
            )

    def test_arbitrary_real_looking_identity_cannot_mint_production_exact_truth(self) -> None:
        assessment = self._assessment()
        if assessment.status is not OutcomeAuthorityStatus.PROVEN_EXHAUSTIVE:
            self.assertIsNone(assessment.authority)
            return

        self.assertIsNotNone(assessment.authority)
        assert assessment.authority is not None
        self._assert_authority_cannot_mint_production_exact_truth(
            assessment.authority
        )

    def test_restart_reverification_does_not_launder_test_only_authority(self) -> None:
        assessment = self._assessment()
        if assessment.status is not OutcomeAuthorityStatus.PROVEN_EXHAUSTIVE:
            self.assertIsNone(assessment.authority)
            return

        authority = assessment.authority
        self.assertIsNotNone(authority)
        assert authority is not None
        raw = authority.to_dict()

        reverified = self._assessment()
        self.assertEqual(
            reverified.status,
            OutcomeAuthorityStatus.PROVEN_EXHAUSTIVE,
        )
        self.assertIsNotNone(reverified.authority)
        assert reverified.authority is not None

        restored = MarketSettlementOutcomeAuthority.from_dict(
            raw,
            verified_authority=reverified.authority,
        )
        self._assert_authority_cannot_mint_production_exact_truth(restored)


if __name__ == "__main__":
    unittest.main()
