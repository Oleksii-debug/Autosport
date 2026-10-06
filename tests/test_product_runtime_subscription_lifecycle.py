from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autosport.causal_collector import CollectorDeltaStore
from autosport.collector_service import (
    CollectorServiceConfig,
    CollectorServiceStoppedError,
    HeadlessCollectorService,
)
from autosport.event_lifecycle import CatalogPage, ContinuousEventLifecycle
from autosport.market_bus import MarketEventBus
from autosport.product_runtime import build_autonomous_product_runtime
from autosport.providers import ProviderUnavailableError


class _Clock:
    def __init__(self) -> None:
        self.value = "2026-10-06T11:30:00+00:00"

    def __call__(self) -> str:
        return self.value


class _Source:
    source_id = "provider-subscription-census"
    stream_epoch = "epoch-1"

    def __init__(self) -> None:
        self.catalog_calls = 0
        self.delta_calls = 0

    def fetch_catalog_page(self, checkpoint):
        self.catalog_calls += 1
        return CatalogPage(
            source_id=self.source_id,
            stream_epoch=self.stream_epoch,
            cursor="catalog-1",
            position=1,
            events=(),
        )

    def fetch_deltas(self, checkpoint, records, max_items):
        self.delta_calls += 1
        return ()

    def resolve_event(self, delta):
        raise AssertionError(
            "subscription lifecycle census does not resolve market deltas"
        )


class _UnavailableCatalogSource(_Source):
    def fetch_catalog_page(self, checkpoint):
        self.catalog_calls += 1
        raise ProviderUnavailableError("provider unavailable for retry lifecycle test")


class _TerminalCatalogSource(_Source):
    def fetch_catalog_page(self, checkpoint):
        self.catalog_calls += 1
        raise RuntimeError("terminal provider/source failure")


class _TrackingMarketEventBus(MarketEventBus):
    instances: list["_TrackingMarketEventBus"] = []

    def __init__(self, store) -> None:
        super().__init__(store)
        type(self).instances.append(self)


def _assert_single_runtime_subscription(bus: _TrackingMarketEventBus) -> None:
    count = len(bus.subscribers)
    if count != 1:
        raise AssertionError(
            "canonical product runtime must own exactly one MarketEventBus subscription; "
            f"observed {count}"
        )


class ProductRuntimeSubscriptionLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        _TrackingMarketEventBus.instances.clear()

    def test_start_stop_restart_never_duplicates_runtime_subscription(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            with patch(
                "autosport.product_runtime.MarketEventBus",
                _TrackingMarketEventBus,
            ):
                runtime = build_autonomous_product_runtime(
                    workspace=root,
                    source=_Source(),
                    clock=clock,
                    sleep=lambda _: None,
                    initial_bankroll="100",
                )
                try:
                    first_bus = _TrackingMarketEventBus.instances[-1]
                    _assert_single_runtime_subscription(first_bus)

                    for cycle in range(6):
                        runtime.start()
                        _assert_single_runtime_subscription(first_bus)
                        runtime.stop(f"subscription-census-{cycle}")
                        _assert_single_runtime_subscription(first_bus)
                finally:
                    runtime.close()

                restored = build_autonomous_product_runtime(
                    workspace=root,
                    source=_Source(),
                    clock=clock,
                    sleep=lambda _: None,
                    initial_bankroll="100",
                )
                try:
                    self.assertEqual(len(_TrackingMarketEventBus.instances), 2)
                    second_bus = _TrackingMarketEventBus.instances[-1]
                    self.assertIsNot(second_bus, first_bus)
                    _assert_single_runtime_subscription(second_bus)

                    restored.start()
                    _assert_single_runtime_subscription(second_bus)
                    restored.stop("subscription-census-restored")
                    _assert_single_runtime_subscription(second_bus)
                finally:
                    restored.close()

    def test_subscription_census_detects_seeded_duplicate_listener(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "autosport.product_runtime.MarketEventBus",
                _TrackingMarketEventBus,
            ):
                runtime = build_autonomous_product_runtime(
                    workspace=Path(directory),
                    source=_Source(),
                    clock=_Clock(),
                    sleep=lambda _: None,
                    initial_bankroll="100",
                )
                try:
                    bus = _TrackingMarketEventBus.instances[-1]
                    _assert_single_runtime_subscription(bus)

                    bus.subscribe(lambda event: None)

                    with self.assertRaisesRegex(
                        AssertionError,
                        "must own exactly one MarketEventBus subscription",
                    ):
                        _assert_single_runtime_subscription(bus)
                finally:
                    runtime.close()

    def test_stop_during_retry_backoff_cannot_spawn_later_retry_after_reopen(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stop_requested = False
            sleeps: list[float] = []
            source = _UnavailableCatalogSource()

            def request_stop_during_backoff(seconds: float) -> None:
                nonlocal stop_requested
                sleeps.append(seconds)
                stop_requested = True

            service = HeadlessCollectorService(
                delta_store=CollectorDeltaStore(root / "collector.json"),
                lifecycle=ContinuousEventLifecycle(root / "catalog.json"),
                source=source,
                state_path=root / "service.json",
                run_id="retry-stop-census",
                config=CollectorServiceConfig(
                    poll_interval_seconds=1,
                    retry_attempts=5,
                    initial_backoff_seconds=1,
                    max_backoff_seconds=4,
                    jitter_fraction=0,
                ),
                clock=_Clock(),
                sleep=request_stop_during_backoff,
                random_value=lambda: 0,
                stop_requested=lambda: stop_requested,
                stop_reason=lambda: "stop_during_provider_backoff",
            )

            result = service.run(max_cycles=1)

            self.assertEqual(result.cycles_executed, 0)
            self.assertIsNone(result.last_cycle)
            self.assertEqual(source.catalog_calls, 1)
            self.assertEqual(source.delta_calls, 0)
            self.assertEqual(sleeps, [1])
            self.assertEqual(
                service.status()["stop_reason"],
                "stop_during_provider_backoff",
            )
            self.assertIsNotNone(service.status()["stopped_at"])
            self.assertEqual(service.status()["provider_failures"], 0)

            reopened_source = _Source()
            reopened = HeadlessCollectorService(
                delta_store=CollectorDeltaStore(root / "collector.json"),
                lifecycle=ContinuousEventLifecycle(root / "catalog.json"),
                source=reopened_source,
                state_path=root / "service.json",
                run_id="retry-stop-census",
                config=CollectorServiceConfig(
                    poll_interval_seconds=1,
                    retry_attempts=5,
                    initial_backoff_seconds=1,
                    max_backoff_seconds=4,
                    jitter_fraction=0,
                ),
                clock=_Clock(),
                sleep=lambda _: self.fail(
                    "no process-local retry/backoff may survive reopen"
                ),
                random_value=lambda: 0,
                stop_requested=lambda: False,
                stop_reason=lambda: "unused",
            )

            with self.assertRaises(CollectorServiceStoppedError):
                reopened.run_cycle()
            self.assertEqual(reopened_source.catalog_calls, 0)
            self.assertEqual(reopened_source.delta_calls, 0)

            reopened.resume()
            resumed = reopened.run_cycle()

            self.assertFalse(resumed.provider_unavailable)
            self.assertEqual(reopened_source.catalog_calls, 1)
            self.assertEqual(reopened_source.delta_calls, 1)
            self.assertIsNone(reopened.status()["stopped_at"])
            self.assertIsNone(reopened.status()["stop_reason"])
            self.assertEqual(reopened.status()["provider_failures"], 0)


    def test_retry_exhaustion_is_bounded_and_reopen_has_no_inherited_backoff(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _UnavailableCatalogSource()
            sleeps: list[float] = []
            service = HeadlessCollectorService(
                delta_store=CollectorDeltaStore(root / "collector.json"),
                lifecycle=ContinuousEventLifecycle(root / "catalog.json"),
                source=source,
                state_path=root / "service.json",
                run_id="retry-exhaustion-census",
                config=CollectorServiceConfig(
                    poll_interval_seconds=1,
                    retry_attempts=3,
                    initial_backoff_seconds=1,
                    max_backoff_seconds=4,
                    jitter_fraction=0,
                ),
                clock=_Clock(),
                sleep=sleeps.append,
                random_value=lambda: 0,
                stop_requested=lambda: False,
                stop_reason=lambda: "unused",
            )

            exhausted = service.run_cycle()

            self.assertTrue(exhausted.provider_unavailable)
            self.assertEqual(source.catalog_calls, 3)
            self.assertEqual(source.delta_calls, 0)
            self.assertEqual(sleeps, [1, 2])
            self.assertEqual(service.status()["cycles_attempted"], 1)
            self.assertEqual(service.status()["cycles_succeeded"], 0)
            self.assertEqual(service.status()["provider_failures"], 1)
            self.assertEqual(
                service.status()["last_error_code"],
                "ProviderUnavailableError",
            )

            reopened_source = _Source()
            reopened = HeadlessCollectorService(
                delta_store=CollectorDeltaStore(root / "collector.json"),
                lifecycle=ContinuousEventLifecycle(root / "catalog.json"),
                source=reopened_source,
                state_path=root / "service.json",
                run_id="retry-exhaustion-census",
                config=CollectorServiceConfig(
                    poll_interval_seconds=1,
                    retry_attempts=3,
                    initial_backoff_seconds=1,
                    max_backoff_seconds=4,
                    jitter_fraction=0,
                ),
                clock=_Clock(),
                sleep=lambda _: self.fail(
                    "retry/backoff state must not survive process-local reopen"
                ),
                random_value=lambda: 0,
                stop_requested=lambda: False,
                stop_reason=lambda: "unused",
            )

            recovered = reopened.run_cycle()

            self.assertFalse(recovered.provider_unavailable)
            self.assertEqual(reopened_source.catalog_calls, 1)
            self.assertEqual(reopened_source.delta_calls, 1)
            self.assertEqual(reopened.status()["cycles_attempted"], 2)
            self.assertEqual(reopened.status()["cycles_succeeded"], 1)
            self.assertEqual(reopened.status()["provider_failures"], 1)
            self.assertIsNone(reopened.status()["last_error_code"])

    def test_terminal_source_failure_is_not_retried_and_reopen_is_fresh(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _TerminalCatalogSource()
            sleeps: list[float] = []
            service = HeadlessCollectorService(
                delta_store=CollectorDeltaStore(root / "collector.json"),
                lifecycle=ContinuousEventLifecycle(root / "catalog.json"),
                source=source,
                state_path=root / "service.json",
                run_id="terminal-failure-census",
                config=CollectorServiceConfig(
                    poll_interval_seconds=1,
                    retry_attempts=5,
                    initial_backoff_seconds=1,
                    max_backoff_seconds=4,
                    jitter_fraction=0,
                ),
                clock=_Clock(),
                sleep=sleeps.append,
                random_value=lambda: 0,
                stop_requested=lambda: False,
                stop_reason=lambda: "unused",
            )

            with self.assertRaisesRegex(
                RuntimeError,
                "terminal provider/source failure",
            ):
                service.run_cycle()

            self.assertEqual(source.catalog_calls, 1)
            self.assertEqual(source.delta_calls, 0)
            self.assertEqual(sleeps, [])
            self.assertEqual(service.status()["provider_failures"], 0)
            self.assertEqual(service.status()["last_error_code"], "RuntimeError")

            reopened_source = _Source()
            reopened = HeadlessCollectorService(
                delta_store=CollectorDeltaStore(root / "collector.json"),
                lifecycle=ContinuousEventLifecycle(root / "catalog.json"),
                source=reopened_source,
                state_path=root / "service.json",
                run_id="terminal-failure-census",
                config=CollectorServiceConfig(
                    poll_interval_seconds=1,
                    retry_attempts=5,
                    initial_backoff_seconds=1,
                    max_backoff_seconds=4,
                    jitter_fraction=0,
                ),
                clock=_Clock(),
                sleep=lambda _: self.fail(
                    "terminal failure must not leave a process-local retry"
                ),
                random_value=lambda: 0,
                stop_requested=lambda: False,
                stop_reason=lambda: "unused",
            )

            recovered = reopened.run_cycle()

            self.assertFalse(recovered.provider_unavailable)
            self.assertEqual(reopened_source.catalog_calls, 1)
            self.assertEqual(reopened_source.delta_calls, 1)
            self.assertIsNone(reopened.status()["last_error_code"])
            self.assertEqual(reopened.status()["provider_failures"], 0)


    def test_post_subscription_construction_failure_releases_runtime_graph(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            with patch(
                "autosport.product_runtime.MarketEventBus",
                _TrackingMarketEventBus,
            ):
                with patch(
                    "autosport.product_runtime.CanonicalDesktopApplication",
                    side_effect=RuntimeError(
                        "seeded post-subscription construction failure"
                    ),
                ):
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "seeded post-subscription construction failure",
                    ):
                        build_autonomous_product_runtime(
                            workspace=root,
                            source=_Source(),
                            clock=clock,
                            sleep=lambda _: None,
                            initial_bankroll="100",
                        )

                self.assertEqual(len(_TrackingMarketEventBus.instances), 1)
                abandoned_bus = _TrackingMarketEventBus.instances[0]
                _assert_single_runtime_subscription(abandoned_bus)

                restored = build_autonomous_product_runtime(
                    workspace=root,
                    source=_Source(),
                    clock=clock,
                    sleep=lambda _: None,
                    initial_bankroll="100",
                )
                try:
                    self.assertEqual(len(_TrackingMarketEventBus.instances), 2)
                    active_bus = _TrackingMarketEventBus.instances[-1]
                    self.assertIsNot(active_bus, abandoned_bus)
                    _assert_single_runtime_subscription(active_bus)

                    restored.start()
                    _assert_single_runtime_subscription(active_bus)
                    restored.stop("post-subscription-construction-recovered")
                    _assert_single_runtime_subscription(active_bus)
                finally:
                    restored.close()


if __name__ == "__main__":
    unittest.main()
