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


class MarketImpliedHistoricalRosterContradictionTests(unittest.TestCase):
    CUTOFF = datetime(2026, 9, 18, 15, 5, tzinfo=timezone.utc)

    @staticmethod
    def _authority():
        assessment = assess_betfair_historical_market_definition_authority(
            market_id="match_odds",
            market_definition={
                "eventId": "event-1",
                "eventTypeId": "2593174",
                "marketType": "MATCH_ODDS",
                "status": "OPEN",
                # Deliberately omit the canonical durable selection "draw".
                "runners": [{"id": "away"}, {"id": "home"}],
            },
            provider_publish_at="2026-09-18T15:00:00Z",
            observed_at="2026-09-18T15:00:01Z",
        )
        return assessment

    @staticmethod
    def _event(
        selection_id: str,
        sequence: int,
        *,
        status: str = "open",
        source_ts: str = "2026-09-18T15:04:00Z",
    ) -> MarketEvent:
        return MarketEvent(
            event_id="event-1",
            market_id="match_odds",
            selection_id=selection_id,
            decimal_odds=Decimal("3.00"),
            observed_ts="2026-09-18T15:04:00Z",
            ingest_ts="2026-09-18T15:04:00Z",
            source_id="betfair_exchange_historical",
            sequence=sequence,
            market_type=MarketType.WINNER,
            status=status,
            source_ts=source_ts,
            sport="table_tennis",
        )

    def test_nonactive_or_stale_known_selection_cannot_disappear_before_roster_check(self) -> None:
        cases = (
            ("suspended", "suspended", "2026-09-18T15:04:00Z"),
            ("stale", "open", "2026-09-18T14:00:00Z"),
        )
        for label, draw_status, draw_source_ts in cases:
            with self.subTest(label=label):
                directory = tempfile.TemporaryDirectory()
                self.addCleanup(directory.cleanup)
                store = SQLiteMarketStore(Path(directory.name) / f"{label}.db")
                self.addCleanup(store.close)

                mirror = MarketMirror()
                mirror.persist_and_apply(store, self._event("away", 1))
                mirror.persist_and_apply(
                    store,
                    self._event(
                        "draw",
                        2,
                        status=draw_status,
                        source_ts=draw_source_ts,
                    ),
                )
                mirror.persist_and_apply(store, self._event("home", 3))

                assessment = self._authority()
                if assessment.status is OutcomeAuthorityStatus.REFUSED:
                    self.assertIsNone(assessment.authority)
                    continue

                self.assertEqual(
                    assessment.status,
                    OutcomeAuthorityStatus.PROVEN_EXHAUSTIVE,
                )
                self.assertIsNotNone(assessment.authority)
                with self.assertRaisesRegex(
                    MarketImpliedBaselineError,
                    "canonical durable market history contradicts verified outcome roster",
                ):
                    build_market_implied_baseline_evidence(
                        cohort_key="row-1",
                        store=store,
                        outcome_authority=assessment.authority,
                        decision_cutoff=self.CUTOFF,
                        max_age=timedelta(minutes=10),
                    )


if __name__ == "__main__":
    unittest.main()
