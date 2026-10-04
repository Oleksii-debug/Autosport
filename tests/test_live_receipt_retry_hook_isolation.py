import tempfile
import unittest
from datetime import timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from autosport.domain import MarketEvent, MarketType
from autosport.storage import SQLiteMarketStore


class LiveReceiptRetryHookIsolationTests(unittest.TestCase):
    def test_retry_hook_cannot_rewrite_trusted_live_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            event = MarketEvent(
                event_id="event-1",
                market_id="winner",
                selection_id="selection-a",
                decimal_odds=Decimal("2.00"),
                observed_ts="2026-10-04T03:00:01+00:00",
                source_id="provider-a",
                sequence=1,
                market_type=MarketType.WINNER,
                ingest_ts="2026-10-04T03:00:02+00:00",
                metadata={"origin": "canonical"},
            )
            def mutate_retry_view(events):
                retry_view = list(events)
                self.assertEqual(len(retry_view), 1)
                retry_view[0].metadata["retry_hook_edit"] = True

            with patch.object(
                store,
                "_before_live_append_attempt",
                side_effect=mutate_retry_view,
            ):
                accepted = store._append_live_batch_accepted([event])

            self.assertEqual(len(accepted), 1)
            trusted = store.trusted_live_events()
            self.assertEqual(len(trusted), 1)
            self.assertEqual(trusted[0].metadata, {"origin": "canonical"})
            self.assertTrue(store.has_trusted_live_receipt(trusted[0]))
            store.close()


if __name__ == "__main__":
    unittest.main()
