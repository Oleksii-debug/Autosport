from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autosport.product_runtime import build_autonomous_product_runtime
from autosport.storage import SQLiteMarketStore


class _Source:
    source_id = "provider-a"
    stream_epoch = "epoch-1"

    def fetch_catalog_page(self, checkpoint):
        raise AssertionError("collector construction failure test must not fetch provider data")

    def fetch_deltas(self, checkpoint, records, max_items):
        raise AssertionError("collector construction failure test must not fetch provider data")

    def resolve_event(self, delta):
        raise AssertionError("construction failure test must not resolve market events")


class _TrackingMarketStore(SQLiteMarketStore):
    instances: list["_TrackingMarketStore"] = []

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.close_calls = 0
        type(self).instances.append(self)

    def close(self) -> None:
        self.close_calls += 1
        super().close()


class ProductRuntimeDownstreamConstructionCleanupFalsifierTests(unittest.TestCase):
    def test_failure_after_successful_mirror_restore_closes_open_market_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _TrackingMarketStore.instances.clear()

            with patch(
                "autosport.product_runtime.SQLiteMarketStore",
                _TrackingMarketStore,
            ), patch(
                "autosport.product_runtime.CanonicalDesktopApplication",
                side_effect=RuntimeError("forced-downstream-construction-failure"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "forced-downstream-construction-failure",
                ):
                    build_autonomous_product_runtime(
                        workspace=root,
                        source=_Source(),
                        initial_bankroll="100",
                    )

            self.assertEqual(len(_TrackingMarketStore.instances), 1)
            store = _TrackingMarketStore.instances[0]
            try:
                self.assertEqual(
                    store.close_calls,
                    1,
                    "construction unwind must close SQLiteMarketStore exactly once",
                )
            finally:
                # Keep this expected-RED falsifier from leaking a real handle in the
                # test runner while the parent implementation still misses cleanup.
                if store.close_calls == 0:
                    store.close()


if __name__ == "__main__":
    unittest.main()
