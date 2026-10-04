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
from autosport.storage import SQLiteMarketStore
from market_outcome_test_support import issue_synthetic_market_outcome_authority


class MarketImpliedHistoricalRosterContradictionTests(unittest.TestCase):
    CUTOFF = datetime(2026, 9, 18, 15, 5, tzinfo=timezone.utc)

    @staticmethod
    def _authority():
        # Deliberately omit canonical durable selection "draw" from this governed
        # synthetic roster so the test reaches the downstream contradiction fence.
        return issue_synthetic_market_outcome_authority(
            event_id="event-1",
            market_id="match_odds",
            selection_ids=("away", "home"),
            causal_cutoff="2026-09-18T15:00:00Z",
            observed_at="2026-09-18T15:00:01Z",
        )

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

                authority = self._authority()
                with self.assertRaisesRegex(
                    MarketImpliedBaselineError,
                    "canonical durable market history contradicts verified outcome roster",
                ):
                    build_market_implied_baseline_evidence(
                        cohort_key="row-1",
                        store=store,
                        outcome_authority=authority,
                        decision_cutoff=self.CUTOFF,
                        max_age=timedelta(minutes=10),
                    )


if __name__ == "__main__":
    unittest.main()
