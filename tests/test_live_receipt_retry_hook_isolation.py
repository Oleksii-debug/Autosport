import tempfile
import unittest
from contextvars import copy_context
from datetime import timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import autosport.storage as storage_module
from autosport.domain import MarketEvent, MarketType
from autosport.storage import SQLiteMarketStore


class LiveReceiptRetryHookIsolationTests(unittest.TestCase):
    def test_post_snapshot_batch_iterator_rebind_cannot_redirect_canonical_persistence(self) -> None:
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
            forged = MarketEvent(
                event_id="forged-event",
                market_id="winner",
                selection_id="selection-z",
                decimal_odds=Decimal("9.99"),
                observed_ts="2026-10-04T03:00:03+00:00",
                source_id="provider-forged",
                sequence=99,
                market_type=MarketType.WINNER,
                ingest_ts="2026-10-04T03:00:04+00:00",
                metadata={"origin": "forged"},
            )
            original_iter = storage_module._LiveReceiptBatch.__iter__

            def forged_iter(_batch):
                return iter((forged,))

            def rebind_after_snapshot(_events):
                storage_module._LiveReceiptBatch.__iter__ = forged_iter

            try:
                with patch.object(
                    store,
                    "_before_live_append_attempt",
                    side_effect=rebind_after_snapshot,
                ):
                    accepted = store._append_live_batch_accepted([event])
            finally:
                storage_module._LiveReceiptBatch.__iter__ = original_iter

            self.assertEqual(accepted, [event])
            self.assertEqual(store.events(), [event])
            self.assertEqual(store.trusted_live_events(), [event])
            self.assertTrue(store.has_trusted_live_receipt(event))
            self.assertEqual(
                store.trusted_live_current_by_source(),
                {(event.source_id, event.quote_key): event},
            )
            store.close()

    def test_copied_retry_context_cannot_replay_carrier_authority_after_abort(self) -> None:
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
            captured_contexts = []
            stashed_batches = []

            def abort_and_capture(events):
                captured_contexts.append(copy_context())
                stashed_batches.append(events)
                raise RuntimeError("abort after copying retry context")

            with patch.object(
                store,
                "_before_live_append_attempt",
                side_effect=abort_and_capture,
            ):
                with self.assertRaisesRegex(RuntimeError, "abort after copying"):
                    store._append_live_batch_accepted([event])

            self.assertEqual(store.events(), [])
            self.assertEqual(store.trusted_live_events(), [])
            self.assertEqual(store.trusted_live_current_by_source(), {})
            self.assertEqual(len(captured_contexts), 1)
            self.assertEqual(len(stashed_batches), 1)

            accepted = captured_contexts[0].run(
                store.append_batch_accepted,
                stashed_batches[0],
            )
            self.assertEqual(len(accepted), 1)
            self.assertFalse(store.has_trusted_live_receipt(accepted[0]))
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
