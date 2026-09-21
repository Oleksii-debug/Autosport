from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autosport.event_lifecycle import CatalogPage
from autosport.market_bus import MarketEventBus
from autosport.product_runtime import build_autonomous_product_runtime


class _Clock:
    def __init__(self) -> None:
        self.value = "2026-09-21T20:00:00+00:00"

    def __call__(self) -> str:
        return self.value


class _Source:
    source_id = "provider-subscription-census"
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
        raise AssertionError("subscription lifecycle census does not resolve market deltas")


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


if __name__ == "__main__":
    unittest.main()
