from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from autosport.continuous_observation import (
    ContinuousObservationConfig,
    run_continuous_observation,
)


class _Provider:
    source_id = "cleanup-fixture"

    def read_batch(self, max_items: int = 1000):
        raise AssertionError("provider I/O is not expected in cleanup precedence tests")


def _config(workspace: Path) -> ContinuousObservationConfig:
    return ContinuousObservationConfig(
        workspace=workspace,
        max_cycles=1,
        max_runtime_seconds=60,
        interval_seconds=1,
        max_backoff_seconds=1,
        max_items=10,
    )


class ContinuousObservationCleanupPrecedenceTests(unittest.TestCase):
    def test_primary_startup_failure_survives_secondary_store_close_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Mock()
            store.close.side_effect = sqlite3.OperationalError("secondary close failure")

            with patch(
                "autosport.continuous_observation.SQLiteMarketStore",
                return_value=store,
            ), patch(
                "autosport.continuous_observation.SourceHealthStore",
                side_effect=LookupError("primary startup failure"),
            ):
                with self.assertRaisesRegex(LookupError, "primary startup failure"):
                    run_continuous_observation(
                        _Provider(),
                        _config(Path(tmp)),
                        monotonic=lambda: 0.0,
                        waiter=lambda _seconds: False,
                        reporter=None,
                    )

            store.close.assert_called_once_with()

    def test_standalone_store_close_failure_remains_observable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Mock()
            store.close.side_effect = sqlite3.OperationalError("close failed")
            stats = SimpleNamespace(
                received=0,
                accepted=0,
                rejected=0,
                cursor="cycle-1",
                health_status="healthy",
            )

            with patch(
                "autosport.continuous_observation.SQLiteMarketStore",
                return_value=store,
            ), patch(
                "autosport.continuous_observation.SourceHealthStore",
            ), patch(
                "autosport.continuous_observation.MarketMirror.from_store",
            ), patch(
                "autosport.continuous_observation.BoundedMirrorInvalidationBuffer",
            ), patch(
                "autosport.continuous_observation.poll_open_market_store_once",
                return_value=stats,
            ), patch(
                "autosport.continuous_observation._drain_invalidation_projection",
                return_value=False,
            ):
                with self.assertRaisesRegex(sqlite3.OperationalError, "close failed"):
                    run_continuous_observation(
                        _Provider(),
                        _config(Path(tmp)),
                        monotonic=lambda: 0.0,
                        waiter=lambda _seconds: False,
                        reporter=None,
                    )

            store.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
