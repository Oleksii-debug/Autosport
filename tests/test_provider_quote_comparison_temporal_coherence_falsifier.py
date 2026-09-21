from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from autosport.domain import MarketEvent
from autosport.market_mirror import MirrorSnapshot
from autosport.provider_quote_comparison import (
    ProviderQuoteComparisonError,
    compare_provider_quotes,
)


_AS_OF = datetime(2026, 9, 21, 13, 0, tzinfo=timezone.utc)


class ProviderQuoteComparisonTemporalCoherenceFalsifier(unittest.TestCase):
    @staticmethod
    def _event(
        source: str,
        odds: str,
        *,
        source_ts: str,
        observed_ts: str,
        ingest_ts: str,
    ) -> MarketEvent:
        return MarketEvent(
            event_id="event-1",
            market_id="match-odds",
            selection_id="home",
            decimal_odds=Decimal(odds),
            observed_ts=observed_ts,
            source_id=source,
            sequence=1,
            status="open",
            source_ts=source_ts,
            ingest_ts=ingest_ts,
            sport="soccer",
            competition_id="league-1",
            market_semantics_id="match-winner-3way-v1",
            provider_source_class="official-api",
        )

    def _assert_fails_closed(self, *events: MarketEvent) -> None:
        snapshot = MirrorSnapshot(revision=9, events=tuple(events))
        try:
            result = compare_provider_quotes(
                snapshot,
                as_of=_AS_OF,
                max_age=timedelta(minutes=2),
            )
        except ProviderQuoteComparisonError:
            return
        self.assertEqual(result, ())

    def test_later_source_clock_cannot_make_stale_local_observation_fresh(self) -> None:
        impossible = self._event(
            "provider-a",
            "2.00",
            source_ts="2026-09-21T12:59:45+00:00",
            observed_ts="2026-09-21T12:30:00+00:00",
            ingest_ts="2026-09-21T12:30:01+00:00",
        )
        valid = self._event(
            "provider-b",
            "2.10",
            source_ts="2026-09-21T12:59:40+00:00",
            observed_ts="2026-09-21T12:59:41+00:00",
            ingest_ts="2026-09-21T12:59:42+00:00",
        )

        # source_ts is preferred for freshness on the parent lineage. Without a
        # causal-order check, the first quote is accepted as 15 seconds old even
        # though its claimed provider timestamp occurs ~30 minutes after the
        # product says it observed/ingested the quote.
        self._assert_fails_closed(impossible, valid)

    def test_ingest_cannot_predate_local_observation(self) -> None:
        impossible = self._event(
            "provider-a",
            "2.00",
            source_ts="2026-09-21T12:59:20+00:00",
            observed_ts="2026-09-21T12:59:40+00:00",
            ingest_ts="2026-09-21T12:59:30+00:00",
        )
        valid = self._event(
            "provider-b",
            "2.10",
            source_ts="2026-09-21T12:59:20+00:00",
            observed_ts="2026-09-21T12:59:30+00:00",
            ingest_ts="2026-09-21T12:59:31+00:00",
        )

        self._assert_fails_closed(impossible, valid)


if __name__ == "__main__":
    unittest.main()
