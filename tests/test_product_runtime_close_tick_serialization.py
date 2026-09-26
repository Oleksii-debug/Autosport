from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from threading import Event, Thread

from autosport.event_lifecycle import CatalogPage
from autosport.product_runtime import build_autonomous_product_runtime


class _Clock:
    def __call__(self) -> str:
        return "2026-09-24T21:10:00+00:00"


class _Source:
    source_id = "provider-a"
    stream_epoch = "epoch-1"

    def fetch_catalog_page(self, checkpoint):
        return CatalogPage(
            source_id=self.source_id,
            stream_epoch=self.stream_epoch,
            cursor="catalog-1",
            position=1,
            events=(),
        )

    def fetch_deltas(self, checkpoint, records, max_items):
        return ()

    def resolve_event(self, delta):
        raise AssertionError("no market delta should be resolved in this test")


class _BlockingCoordinator:
    """Expose the check-then-act window after runtime authority preflight."""

    def __init__(self, inner, *, tick_entered: Event, allow_tick_return: Event) -> None:
        self._inner = inner
        self._tick_entered = tick_entered
        self._allow_tick_return = allow_tick_return
        self.tick_effects = 0

    def status(self):
        return self._inner.status()

    def tick(self):
        self._tick_entered.set()
        if not self._allow_tick_return.wait(timeout=5):
            raise AssertionError("test did not release the in-flight runtime tick")
        self.tick_effects += 1
        return object()

    def __getattr__(self, name):
        return getattr(self._inner, name)


class ProductRuntimeCloseTickSerializationTests(unittest.TestCase):
    def test_close_cannot_release_runtime_lease_during_admitted_tick(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = build_autonomous_product_runtime(
                workspace=Path(directory),
                source=_Source(),
                clock=_Clock(),
                sleep=lambda _: None,
                initial_bankroll="100",
            )
            tick_entered = Event()
            allow_tick_return = Event()
            release_entered = Event()
            tick_errors: list[BaseException] = []
            close_errors: list[BaseException] = []

            runtime.coordinator = _BlockingCoordinator(
                runtime.coordinator,
                tick_entered=tick_entered,
                allow_tick_return=allow_tick_return,
            )
            original_release = runtime._runtime_lease.release

            def observed_release() -> None:
                release_entered.set()
                original_release()

            runtime._runtime_lease.release = observed_release  # type: ignore[method-assign]

            def run_tick() -> None:
                try:
                    runtime.tick()
                except BaseException as exc:  # pragma: no cover - asserted below
                    tick_errors.append(exc)

            def run_close() -> None:
                try:
                    runtime.close()
                except BaseException as exc:  # pragma: no cover - asserted below
                    close_errors.append(exc)

            tick_thread = Thread(target=run_tick, name="runtime-tick")
            close_thread = Thread(target=run_close, name="runtime-close")
            tick_thread.start()
            self.assertTrue(tick_entered.wait(timeout=2))
            close_thread.start()

            try:
                self.assertFalse(
                    release_entered.wait(timeout=1),
                    "close released product runtime authority while an admitted tick "
                    "was still inside its coordinator effect",
                )
            finally:
                allow_tick_return.set()
                tick_thread.join(timeout=2)
                close_thread.join(timeout=2)

            self.assertFalse(tick_thread.is_alive())
            self.assertFalse(close_thread.is_alive())
            self.assertEqual(tick_errors, [])
            self.assertEqual(close_errors, [])
            self.assertEqual(runtime.coordinator.tick_effects, 1)
            self.assertTrue(release_entered.is_set())


if __name__ == "__main__":
    unittest.main()
