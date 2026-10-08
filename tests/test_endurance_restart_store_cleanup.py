import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import autosport.endurance as endurance
from autosport.storage import SQLiteMarketStore as RealSQLiteMarketStore


class EnduranceRestartStoreCleanupTests(unittest.TestCase):
    def test_restart_store_closes_before_restart_read_failure_escapes(self) -> None:
        config = endurance.EnduranceConfig(
            event_count=20,
            quote_keys=10,
            batch_size=10,
            restart_cycles=1,
            paper_tickets=5,
        )
        opened: list[RealSQLiteMarketStore] = []
        closed_ids: set[int] = set()
        events_calls = 0
        real_events = RealSQLiteMarketStore.events
        real_close = RealSQLiteMarketStore.close

        def tracked_store(path: str | Path) -> RealSQLiteMarketStore:
            store = RealSQLiteMarketStore(path)
            opened.append(store)
            return store

        def fail_on_restart_events(
            store: RealSQLiteMarketStore,
            event_id: str | None = None,
        ):
            nonlocal events_calls
            events_calls += 1
            if events_calls == 2:
                raise RuntimeError("simulated restart read failure")
            return real_events(store, event_id)

        def tracked_close(store: RealSQLiteMarketStore) -> None:
            closed_ids.add(id(store))
            real_close(store)

        restart_closed_before_cleanup = False
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(endurance, "SQLiteMarketStore", new=tracked_store),
                patch.object(RealSQLiteMarketStore, "events", new=fail_on_restart_events),
                patch.object(RealSQLiteMarketStore, "close", new=tracked_close),
            ):
                try:
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "simulated restart read failure",
                    ):
                        endurance.run_endurance(Path(tmp), config)

                    self.assertGreaterEqual(len(opened), 2)
                    restart_closed_before_cleanup = id(opened[1]) in closed_ids
                finally:
                    for store in opened:
                        if id(store) not in closed_ids:
                            real_close(store)

        self.assertTrue(
            restart_closed_before_cleanup,
            "restart SQLiteMarketStore leaked when restart read failed",
        )


if __name__ == "__main__":
    unittest.main()
