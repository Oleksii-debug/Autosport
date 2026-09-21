import copy
import unittest

from autosport.market_outcome_lineage import (
    OutcomeRevisionRelation,
    validate_market_outcome_authority_revision,
)
from autosport.market_outcomes import (
    MarketSettlementOutcomeAuthority,
    OutcomeAuthorityStatus,
    assess_betfair_historical_market_definition_authority,
)


class MarketOutcomeRevisionLineageTests(unittest.TestCase):
    @staticmethod
    def _authority(
        *,
        market_id: str = "match_odds",
        publish_at: str = "2026-09-18T15:00:00Z",
        observed_at: str = "2026-09-18T15:00:01Z",
        selection_ids: tuple[str, ...] = ("away", "home"),
    ) -> MarketSettlementOutcomeAuthority:
        assessment = assess_betfair_historical_market_definition_authority(
            market_id=market_id,
            market_definition={
                "eventId": "event-1",
                "eventTypeId": "2593174",
                "marketType": "MATCH_ODDS",
                "status": "OPEN",
                "runners": [{"id": value} for value in selection_ids],
            },
            provider_publish_at=publish_at,
            observed_at=observed_at,
        )
        if assessment.status is not OutcomeAuthorityStatus.PROVEN_EXHAUSTIVE:
            raise AssertionError("test fixture did not produce verified authority")
        if assessment.authority is None:
            raise AssertionError("verified test fixture lacks authority")
        return assessment.authority

    def test_exact_replay_is_same_revision(self):
        authority = self._authority()

        decision = validate_market_outcome_authority_revision(authority, authority)

        self.assertIs(decision.relation, OutcomeRevisionRelation.SAME_REVISION)
        self.assertEqual(
            decision.previous_authority_sha256,
            decision.current_authority_sha256,
        )

    def test_same_provider_revision_may_be_reobserved_later(self):
        previous = self._authority(observed_at="2026-09-18T15:00:01Z")
        current = self._authority(observed_at="2026-09-18T15:03:00Z")
        self.assertNotEqual(previous.authority_sha256, current.authority_sha256)

        decision = validate_market_outcome_authority_revision(previous, current)

        self.assertIs(decision.relation, OutcomeRevisionRelation.SAME_REVISION)
        self.assertEqual(previous.source_revision, current.source_revision)
        self.assertEqual(
            previous.roster_provenance_sha256,
            current.roster_provenance_sha256,
        )

    def test_strictly_newer_provider_cutoff_is_successor(self):
        previous = self._authority()
        current = self._authority(
            publish_at="2026-09-18T15:05:00Z",
            observed_at="2026-09-18T15:05:01Z",
            selection_ids=("away", "draw", "home"),
        )

        decision = validate_market_outcome_authority_revision(previous, current)

        self.assertIs(decision.relation, OutcomeRevisionRelation.STRICT_SUCCESSOR)
        self.assertNotEqual(previous.source_revision, current.source_revision)
        self.assertNotEqual(previous.selection_ids, current.selection_ids)

    def test_older_causal_cutoff_cannot_replace_newer_authority(self):
        previous = self._authority(
            publish_at="2026-09-18T15:05:00Z",
            observed_at="2026-09-18T15:05:01Z",
        )
        stale = self._authority(
            publish_at="2026-09-18T15:04:00Z",
            observed_at="2026-09-18T15:06:00Z",
        )

        with self.assertRaisesRegex(ValueError, "causal cutoff rollback"):
            validate_market_outcome_authority_revision(previous, stale)

    def test_newer_provider_revision_with_backdated_observation_is_rejected(self):
        previous = self._authority(
            publish_at="2026-09-18T15:00:00Z",
            observed_at="2026-09-18T15:10:00Z",
        )
        backdated_arrival = self._authority(
            publish_at="2026-09-18T15:05:00Z",
            observed_at="2026-09-18T15:06:00Z",
        )

        with self.assertRaisesRegex(ValueError, "observed_at moved backwards"):
            validate_market_outcome_authority_revision(previous, backdated_arrival)

    def test_same_cutoff_with_different_roster_is_conflicting_evidence(self):
        previous = self._authority(selection_ids=("away", "home"))
        conflicting = self._authority(selection_ids=("away", "draw", "home"))

        with self.assertRaisesRegex(
            ValueError,
            "conflicting market outcome evidence at identical causal cutoff",
        ):
            validate_market_outcome_authority_revision(previous, conflicting)

    def test_lineage_cannot_cross_provider_market_identity(self):
        previous = self._authority(market_id="match_odds")
        other_market = self._authority(market_id="other_match_odds")

        with self.assertRaisesRegex(ValueError, "lineage identity mismatch"):
            validate_market_outcome_authority_revision(previous, other_market)

    def test_settlement_or_verification_semantic_drift_is_rejected(self):
        previous = self._authority()

        changed_rules = copy.copy(
            self._authority(
                publish_at="2026-09-18T15:05:00Z",
                observed_at="2026-09-18T15:05:01Z",
            )
        )
        object.__setattr__(changed_rules, "settlement_rules_sha256", "d" * 64)
        with self.assertRaisesRegex(ValueError, "settlement rules changed"):
            validate_market_outcome_authority_revision(previous, changed_rules)

        changed_protocol = copy.copy(
            self._authority(
                publish_at="2026-09-18T15:05:00Z",
                observed_at="2026-09-18T15:05:01Z",
            )
        )
        object.__setattr__(
            changed_protocol,
            "verification_protocol_sha256",
            "e" * 64,
        )
        with self.assertRaisesRegex(ValueError, "verification protocol changed"):
            validate_market_outcome_authority_revision(previous, changed_protocol)


if __name__ == "__main__":
    unittest.main()
