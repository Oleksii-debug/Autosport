from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.domain import MarketEvent
from autosport.market_mirror import MirrorSnapshot
from autosport.registered_strategy_live_feature import (
    LIVE_FEATURE_DEFINITION_SHA256,
    LIVE_FEATURE_SET_ID,
    LIVE_FEATURE_SET_VERSION,
    LIVE_FEATURE_SOURCE_SHA256,
    RegisteredStrategyLiveFeatureError,
    observe_registered_strategy_live_features,
)
from autosport.scientific_registry import FeatureSet, ModelVersion, ScientificRegistry


class RegisteredStrategyLiveFeatureSourceTimestampCausalityTests(unittest.TestCase):
    def _registry(self, root: Path) -> ScientificRegistry:
        registry = ScientificRegistry.initialize_pristine(
            root / "scientific_registry.json"
        )
        registry.append(
            FeatureSet(
                feature_set_id=LIVE_FEATURE_SET_ID,
                version=LIVE_FEATURE_SET_VERSION,
                definition_sha256=LIVE_FEATURE_DEFINITION_SHA256,
                source_sha256=LIVE_FEATURE_SOURCE_SHA256,
                available_at_utc="2026-01-01T00:00:00Z",
            )
        )
        registry.append(
            ModelVersion(
                model_version_id="model-v1",
                model_family="mean-baseline-v1",
                artifact_sha256="a" * 64,
                source_sha256="b" * 64,
                environment_sha256="c" * 64,
                dataset_snapshot_id="dataset-v1",
                feature_set_id=LIVE_FEATURE_SET_ID,
                research_protocol_id="protocol-v1",
                seed=7,
                config_sha256="d" * 64,
                created_at="2026-01-02T00:00:00Z",
            )
        )
        return registry

    @staticmethod
    def _event(*, observed_ts: str, ingest_ts: str, source_ts: str) -> MarketEvent:
        return MarketEvent(
            event_id="event-a",
            market_id="market-a",
            selection_id="selection-a",
            decimal_odds=Decimal("2"),
            observed_ts=observed_ts,
            source_id="provider-a",
            sequence=1,
            status="open",
            source_ts=source_ts,
            ingest_ts=ingest_ts,
            sport="football",
        )

    def test_later_provider_clock_cannot_launder_stale_local_quote(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = self._registry(Path(directory))
            snapshot = MirrorSnapshot(
                revision=1,
                events=(
                    self._event(
                        observed_ts="2026-01-03T10:00:00Z",
                        ingest_ts="2026-01-03T10:00:01Z",
                        source_ts="2026-01-03T10:09:30Z",
                    ),
                ),
            )

            with self.assertRaisesRegex(
                RegisteredStrategyLiveFeatureError,
                "provider source timestamp is after local observation",
            ):
                observe_registered_strategy_live_features(
                    "input-a",
                    snapshot,
                    decision_at="2026-01-03T10:10:00Z",
                    freshness_max_age_seconds=60,
                    registry=registry,
                    model_version_id="model-v1",
                )

    def test_causally_prior_provider_timestamp_remains_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = self._registry(Path(directory))
            snapshot = MirrorSnapshot(
                revision=1,
                events=(
                    self._event(
                        observed_ts="2026-01-03T10:00:00Z",
                        ingest_ts="2026-01-03T10:00:01Z",
                        source_ts="2026-01-03T09:59:45Z",
                    ),
                ),
            )

            observations = observe_registered_strategy_live_features(
                "input-a",
                snapshot,
                decision_at="2026-01-03T10:00:30Z",
                freshness_max_age_seconds=60,
                registry=registry,
                model_version_id="model-v1",
            )

            self.assertEqual(len(observations), 1)
            self.assertEqual(
                observations[0].freshness_timestamp,
                "2026-01-03T09:59:45Z",
            )


if __name__ == "__main__":
    unittest.main()
