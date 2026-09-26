from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from autosport.domain import MarketEvent, MarketType
from autosport.market_implied_baseline import (
    MarketImpliedBaselineError,
    build_market_implied_baseline_evidence,
)
from autosport.market_mirror import MarketMirror
from autosport.market_outcomes import (
    OutcomeAuthorityStatus,
    assess_betfair_historical_market_definition_authority,
)
from autosport.storage import SQLiteMarketStore


class MarketImpliedOutcomeRosterOriginFalsifierTests(unittest.TestCase):
    """A caller-authored runner roster must not define scientific completeness."""

    def test_caller_smaller_roster_cannot_normalize_canonical_three_way_market(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = SQLiteMarketStore(Path(directory.name) / "market.db")
        self.addCleanup(store.close)

        mirror = MarketMirror()
        for sequence, (selection_id, odds) in enumerate(
            (("away", "3.00"), ("draw", "3.00"), ("home", "3.00")),
            start=1,
        ):
            mirror.persist_and_apply(
                store,
                MarketEvent(
                    event_id="event-1",
                    market_id="match_odds",
                    selection_id=selection_id,
                    decimal_odds=Decimal(odds),
                    observed_ts="2026-09-18T15:04:00Z",
                    ingest_ts="2026-09-18T15:04:00Z",
                    source_id="betfair_exchange_historical",
                    sequence=sequence,
                    market_type=MarketType.WINNER,
                    status="open",
                    source_ts="2026-09-18T15:04:00Z",
                    sport="table_tennis",
                ),
            )

        assessment = assess_betfair_historical_market_definition_authority(
            market_id="match_odds",
            market_definition={
                "eventId": "event-1",
                "eventTypeId": "2593174",
                "marketType": "MATCH_ODDS",
                "status": "OPEN",
                # Deliberately omit canonical durable selection "draw".
                "runners": [{"id": "away"}, {"id": "home"}],
            },
            provider_publish_at="2026-09-18T15:00:00Z",
            observed_at="2026-09-18T15:00:01Z",
        )

        # A future origin-bound issuer may correctly refuse this caller assertion.
        if assessment.status is OutcomeAuthorityStatus.REFUSED:
            self.assertIsNone(assessment.authority)
            return

        self.assertEqual(
            assessment.status,
            OutcomeAuthorityStatus.PROVEN_EXHAUSTIVE,
            "unexpected outcome-authority state",
        )
        self.assertIsNotNone(assessment.authority)

        # If the generic issuer still returns a positive authority, the scientific
        # baseline must independently reject it rather than using its reduced runner
        # list to filter the canonical three-way quote history down to two selections.
        with self.assertRaises(
            (MarketImpliedBaselineError, TypeError, ValueError),
            msg=(
                "caller-authored smaller marketDefinition minted positive exhaustive "
                "authority and was accepted as a complete market-implied baseline"
            ),
        ):
            build_market_implied_baseline_evidence(
                cohort_key="row-1",
                store=store,
                outcome_authority=assessment.authority,
                decision_cutoff=datetime(
                    2026, 9, 18, 15, 5, tzinfo=timezone.utc
                ),
                max_age=timedelta(minutes=10),
            )


if __name__ == "__main__":
    unittest.main()
