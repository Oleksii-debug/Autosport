from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autosport.causal_collector import CanonicalDesktopApplication
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


class ProductRuntimeDownstreamConstructionCleanupFalsifierTests(unittest.TestCase):
    def test_failure_after_successful_mirror_restore_closes_open_market_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stores = []
            close_calls = []
            original_close = SQLiteMarketStore.close

            def tracked_close(store):
                close_calls.append(store)
                return original_close(store)

            def fail_application_init(_self, market_bus, *_args, **_kwargs):
                stores.append(market_bus.store)
                raise RuntimeError("forced-downstream-construction-failure")

            with (
                patch.object(SQLiteMarketStore, "close", tracked_close),
                patch.object(
                    CanonicalDesktopApplication,
                    "__init__",
                    fail_application_init,
                ),
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

            self.assertEqual(len(stores), 1)
            self.assertEqual(
                close_calls.count(stores[0]),
                1,
                "construction unwind must close SQLiteMarketStore exactly once",
            )


if __name__ == "__main__":
    unittest.main()
