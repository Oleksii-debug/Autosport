from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from autosport.domain import MarketEvent
from autosport.market_mirror import MarketMirror
from autosport.storage import SQLiteMarketStore


BOUNDARY = datetime(2026, 9, 21, 8, 30, tzinfo=timezone.utc)
MAX_AGE = timedelta(seconds=5)


def _event(
    selection: str,
    *,
    observed_ts: str,
    ingest_ts: str,
    source_ts: str = "2026-09-21T08:29:58+00:00",
) -> MarketEvent:
    return MarketEvent(
        event_id="event-1",
        market_id="market-1",
        selection_id=selection,
        decimal_odds=Decimal("2.00"),
        observed_ts=observed_ts,
        source_id="provider-a",
        sequence=1,
        status="open",
        source_ts=source_ts,
        ingest_ts=ingest_ts,
    )


class MarketMirrorCausalCutoffTests(unittest.TestCase):
    def test_live_view_rejects_future_local_receipt_despite_fresh_source_time(self) -> None:
        mirror = MarketMirror()
        mirror.apply(
            _event(
                "future-observed",
                observed_ts="2026-09-21T08:30:10+00:00",
                ingest_ts="2026-09-21T08:30:11+00:00",
            )
        )
        mirror.apply(
            _event(
                "future-ingest",
                observed_ts="2026-09-21T08:29:59+00:00",
                ingest_ts="2026-09-21T08:30:01+00:00",
            )
        )
        mirror.apply(
            _event(
                "eligible",
                observed_ts="2026-09-21T08:29:59+00:00",
                ingest_ts="2026-09-21T08:30:00+00:00",
            )
        )

        active = mirror.active_view(as_of=BOUNDARY, max_age=MAX_AGE)

        self.assertEqual(
            tuple(event.selection_id for event in active.events),
            ("eligible",),
        )
        self.assertEqual(len(mirror.view().events), 3)

    def test_focused_key_view_uses_the_same_causal_cutoff(self) -> None:
        mirror = MarketMirror()
        future = _event(
            "future-ingest",
            observed_ts="2026-09-21T08:29:59+00:00",
            ingest_ts="2026-09-21T08:30:01+00:00",
        )
        eligible = _event(
            "eligible",
            observed_ts="2026-09-21T08:29:59+00:00",
            ingest_ts="2026-09-21T08:30:00+00:00",
        )
        mirror.apply(future)
        mirror.apply(eligible)

        active = mirror.active_view_for_keys(
            {
                (future.source_id, future.quote_key),
                (eligible.source_id, eligible.quote_key),
            },
            as_of=BOUNDARY,
            max_age=MAX_AGE,
        )

        self.assertEqual(
            tuple(event.selection_id for event in active.events),
            ("eligible",),
        )

    def test_live_and_replay_views_agree_on_local_knowledge_cutoff(self) -> None:
        future = _event(
            "future-local",
            observed_ts="2026-09-21T08:30:10+00:00",
            ingest_ts="2026-09-21T08:30:11+00:00",
        )
        eligible = _event(
            "eligible",
            observed_ts="2026-09-21T08:29:59+00:00",
            ingest_ts="2026-09-21T08:30:00+00:00",
        )
        mirror = MarketMirror()
        mirror.apply(future)
        mirror.apply(eligible)

        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                store.append_many([future, eligible])
                replay = MarketMirror.replay_view_from_store(
                    store,
                    as_of=BOUNDARY,
                    max_age=MAX_AGE,
                )
            finally:
                store.close()

        live = mirror.active_view(as_of=BOUNDARY, max_age=MAX_AGE)
        self.assertEqual(
            tuple(event.selection_id for event in live.events),
            ("eligible",),
        )
        self.assertEqual(
            tuple(event.selection_id for event in replay.events),
            tuple(event.selection_id for event in live.events),
        )

    def test_provider_source_freshness_remains_independent_from_local_cutoff(self) -> None:
        mirror = MarketMirror()
        mirror.apply(
            _event(
                "source-stale",
                observed_ts="2026-09-21T08:29:59+00:00",
                ingest_ts="2026-09-21T08:30:00+00:00",
                source_ts="2026-09-21T08:29:54+00:00",
            )
        )
        mirror.apply(
            _event(
                "source-fresh",
                observed_ts="2026-09-21T08:29:58+00:00",
                ingest_ts="2026-09-21T08:29:59+00:00",
                source_ts="2026-09-21T08:29:56+00:00",
            )
        )

        active = mirror.active_view(as_of=BOUNDARY, max_age=MAX_AGE)

        self.assertEqual(
            tuple(event.selection_id for event in active.events),
            ("source-fresh",),
        )


if __name__ == "__main__":
    unittest.main()
