from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from autosport.continuous_session import SessionState
from autosport.event_lifecycle import CatalogPage
from autosport.product_runtime import build_autonomous_product_runtime


class _EmptySource:
    source_id = "resource-endurance-source"
    stream_epoch = "epoch-1"

    def fetch_catalog_page(self, checkpoint):
        position = 1 if checkpoint is None else int(checkpoint.position) + 1
        return CatalogPage(
            source_id=self.source_id,
            stream_epoch=self.stream_epoch,
            cursor=f"catalog-{position}",
            position=position,
            events=(),
        )

    def fetch_deltas(self, checkpoint, records, max_items):
        return ()

    def resolve_event(self, delta):
        raise AssertionError("empty endurance source must not resolve a market delta")


class ProductRuntimeResourceEnduranceTests(unittest.TestCase):
    def test_repeated_reopen_tick_stop_close_releases_workspace_handles(self) -> None:
        """Repeated canonical restarts must not strand the durable workspace open.

        Renaming the complete workspace after every close is intentionally part of the
        gate: on Windows an outstanding SQLite/file handle makes the rename fail, while
        the next build proves the moved-back durable state is still reusable.
        """

        restart_cycles = 24
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "product"
            session_id: str | None = None

            for expected_cycle in range(1, restart_cycles + 1):
                runtime = build_autonomous_product_runtime(
                    workspace=workspace,
                    source=_EmptySource(),
                    initial_bankroll="100",
                )
                started = runtime.start()
                self.assertEqual(started.state, SessionState.RUNNING)
                if session_id is None:
                    session_id = started.session_id
                else:
                    self.assertEqual(started.session_id, session_id)

                tick = runtime.tick()
                self.assertEqual(tick.session_id, session_id)
                self.assertEqual(tick.cycle_index, expected_cycle)

                stopped = runtime.stop(f"resource_endurance_cycle_{expected_cycle}")
                self.assertEqual(stopped.state, SessionState.STOPPED)
                self.assertEqual(stopped.session_id, session_id)
                self.assertEqual(stopped.cycles_completed, expected_cycle)
                runtime.close()

                moved = root / f"product-move-probe-{expected_cycle}"
                workspace.rename(moved)
                self.assertFalse(workspace.exists())
                moved.rename(workspace)
                self.assertTrue(workspace.exists())

            final_runtime = build_autonomous_product_runtime(
                workspace=workspace,
                source=_EmptySource(),
                initial_bankroll="100",
            )
            final_status = final_runtime.status()
            self.assertEqual(final_status.session_id, session_id)
            self.assertEqual(final_status.state, SessionState.STOPPED)
            self.assertEqual(final_status.cycles_completed, restart_cycles)
            final_runtime.close()

            final_probe = root / "product-final-move-probe"
            workspace.rename(final_probe)
            final_probe.rename(workspace)


if __name__ == "__main__":
    unittest.main()
