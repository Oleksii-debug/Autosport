import tempfile
import unittest
from datetime import timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from autosport.domain import MarketEvent, MarketType
from autosport.storage import SQLiteMarketStore


class LiveReceiptRetryHookIsolationTests(unittest.TestCase):
    def test_stashed_retry_view_loses_authority_after_live_context_exits(self) -> None:
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
            stashed = []
            
            def abort_and_stash(events):
                stashed.append(events)
                raise RuntimeError("abort after stashing retry view")

            with patch.object(
                store,
                "append_batch_accepted",
                side_effect=abort_and_stash,
            ):
                with self.assertRaisesRegex(RuntimeError, "abort after stashing"):
                    store._append_live_batch_accepted([event])

            self.assertEqual(store.events(), [])
            self.assertEqual(store.trusted_live_events(), [])
            self.assertEqual(len(stashed), 1)

            leaked_event = stashed[0][0]
            object.__setattr__(leaked_event, "sequence", 2)
            self.assertEqual(store.append_batch_accepted(stashed[0]), [leaked_event])
            self.assertFalse(store.has_trusted_live_receipt(leaked_event))
            self.assertEqual(store.trusted_live_current_by_source(), {})
            store.close()

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
            canonical_append = store.append_batch_accepted

            def mutate_retry_view(events):
                retry_view = list(events)
                self.assertEqual(len(retry_view), 1)
                retry_view[0].metadata["retry_hook_edit"] = True
                return canonical_append(events)

            with patch.object(
                store,
                "append_batch_accepted",
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
