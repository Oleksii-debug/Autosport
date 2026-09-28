from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from threading import Event, Thread

from autosport.event_lifecycle import CatalogPage
from autosport.product_runtime import ProductCompositionError, build_autonomous_product_runtime


class _Clock:
    def __init__(self) -> None:
        self.value = "2026-09-21T17:20:00+00:00"

    def __call__(self) -> str:
        return self.value


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


class ProductRuntimeCallerLeaseReleaseFalsifierTests(unittest.TestCase):
    def test_caller_cannot_release_live_runtime_lease_and_keep_runtime_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = build_autonomous_product_runtime(
                workspace=root,
                source=_Source(),
                clock=_Clock(),
                sleep=lambda _: None,
                initial_bankroll="100",
            )
            second = None
            try:
                first._runtime_lease.release()

                second = build_autonomous_product_runtime(
                    workspace=root,
                    source=_Source(),
                    clock=_Clock(),
                    sleep=lambda _: None,
                    initial_bankroll="100",
                )

                with self.assertRaisesRegex(
                    ProductCompositionError,
                    "no longer owns workspace authority|workspace authority",
                ):
                    first.tick()
            finally:
                if second is not None:
                    second.close()
                first.close()

    def test_direct_lease_release_cannot_cross_an_admitted_tick(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = build_autonomous_product_runtime(
                workspace=root,
                source=_Source(),
                clock=_Clock(),
                sleep=lambda _: None,
                initial_bankroll="100",
            )
            tick_entered = Event()
            allow_tick_return = Event()
            release_started = Event()
            release_finished = Event()
            tick_errors: list[BaseException] = []
            release_errors: list[BaseException] = []
            first.coordinator = _BlockingCoordinator(
                first.coordinator,
                tick_entered=tick_entered,
                allow_tick_return=allow_tick_return,
            )

            def run_tick() -> None:
                try:
                    first.tick()
                except BaseException as exc:  # pragma: no cover - asserted below
                    tick_errors.append(exc)

            def run_release() -> None:
                release_started.set()
                try:
                    first._runtime_lease.release()
                except BaseException as exc:  # pragma: no cover - asserted below
                    release_errors.append(exc)
                finally:
                    release_finished.set()

            tick_thread = Thread(target=run_tick, name="runtime-tick")
            release_thread = Thread(target=run_release, name="runtime-direct-release")
            tick_thread.start()
            self.assertTrue(tick_entered.wait(timeout=2))
            release_thread.start()
            self.assertTrue(release_started.wait(timeout=2))

            try:
                self.assertFalse(
                    release_finished.wait(timeout=1),
                    "direct lease release crossed an admitted runtime tick",
                )
                with self.assertRaisesRegex(
                    ProductCompositionError,
                    "another Autosport product runtime already owns this workspace",
                ):
                    build_autonomous_product_runtime(
                        workspace=root,
                        source=_Source(),
                        clock=_Clock(),
                        sleep=lambda _: None,
                        initial_bankroll="100",
                    )
            finally:
                allow_tick_return.set()
                tick_thread.join(timeout=2)
                release_thread.join(timeout=2)

            self.assertFalse(tick_thread.is_alive())
            self.assertFalse(release_thread.is_alive())
            self.assertEqual(tick_errors, [])
            self.assertEqual(release_errors, [])
            self.assertEqual(first.coordinator.tick_effects, 1)
            self.assertTrue(release_finished.is_set())

            second = build_autonomous_product_runtime(
                workspace=root,
                source=_Source(),
                clock=_Clock(),
                sleep=lambda _: None,
                initial_bankroll="100",
            )
            try:
                with self.assertRaisesRegex(
                    ProductCompositionError,
                    "no longer owns workspace authority|workspace authority",
                ):
                    first.tick()
            finally:
                second.close()
                first.close()


if __name__ == "__main__":
    unittest.main()
