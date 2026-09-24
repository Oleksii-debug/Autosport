from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.domain import MarketEvent
from autosport.market_mirror import MirrorSnapshot
from autosport.registered_strategy_live_feature import (
    LIVE_FEATURE_DEFINITION_JSON,
    LIVE_FEATURE_DEFINITION_SHA256,
    LIVE_FEATURE_SET_ID,
    LIVE_FEATURE_SET_VERSION,
    LIVE_FEATURE_SOURCE_CONTRACT_JSON,
    LIVE_FEATURE_SOURCE_SHA256,
    RegisteredStrategyLiveFeatureError,
    market_snapshot_sha256,
    observe_registered_strategy_live_features,
    resolve_registered_live_feature_authority,
)
from autosport.scientific_registry import FeatureSet, ModelVersion, ScientificRegistry


FEATURE_AVAILABLE_AT = "2026-01-01T00:00:00Z"
MODEL_AVAILABLE_AT = "2026-01-02T00:00:00Z"
DECISION_AT = "2026-01-03T00:00:10Z"


def _registry(
    root: Path,
    *,
    feature_definition_sha256: str = LIVE_FEATURE_DEFINITION_SHA256,
    feature_source_sha256: str = LIVE_FEATURE_SOURCE_SHA256,
    feature_available_at: str = FEATURE_AVAILABLE_AT,
    model_available_at: str = MODEL_AVAILABLE_AT,
    model_feature_set_id: str = LIVE_FEATURE_SET_ID,
) -> ScientificRegistry:
    registry = ScientificRegistry.initialize_pristine(
        root / "scientific_registry.json"
    )
    registry.append(
        FeatureSet(
            feature_set_id=LIVE_FEATURE_SET_ID,
            version=LIVE_FEATURE_SET_VERSION,
            definition_sha256=feature_definition_sha256,
            source_sha256=feature_source_sha256,
            available_at_utc=feature_available_at,
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
            feature_set_id=model_feature_set_id,
            research_protocol_id="protocol-v1",
            seed=7,
            config_sha256="d" * 64,
            created_at=model_available_at,
        )
    )
    return registry


def _event(
    *,
    event_id: str,
    selection_id: str,
    decimal_odds: str,
    observed_ts: str = "2026-01-03T00:00:01Z",
    ingest_ts: str = "2026-01-03T00:00:02Z",
    source_ts: str | None = "2026-01-03T00:00:00Z",
    status: str = "open",
    exchange_side: str | None = None,
) -> MarketEvent:
    return MarketEvent(
        event_id=event_id,
        market_id="market-a",
        selection_id=selection_id,
        decimal_odds=Decimal(decimal_odds),
        observed_ts=observed_ts,
        source_id="provider-a",
        sequence=1,
        status=status,
        source_ts=source_ts,
        ingest_ts=ingest_ts,
        sport="football",
        exchange_side=exchange_side,
    )


class RegisteredStrategyLiveFeatureTests(unittest.TestCase):
    def test_contract_digests_are_frozen_and_self_consistent(self) -> None:
        self.assertEqual(
            hashlib.sha256(LIVE_FEATURE_DEFINITION_JSON.encode("utf-8")).hexdigest(),
            LIVE_FEATURE_DEFINITION_SHA256,
        )
        self.assertEqual(
            LIVE_FEATURE_DEFINITION_SHA256,
            "0fd30dc53fef6ec49eee062f8f90761947646313d38cde28f77780f02dd470fd",
        )
        source_contract = json.loads(LIVE_FEATURE_SOURCE_CONTRACT_JSON)
        self.assertEqual(
            source_contract["feature_definition_sha256"],
            LIVE_FEATURE_DEFINITION_SHA256,
        )
        self.assertEqual(
            hashlib.sha256(
                LIVE_FEATURE_SOURCE_CONTRACT_JSON.encode("utf-8")
            ).hexdigest(),
            LIVE_FEATURE_SOURCE_SHA256,
        )
        self.assertEqual(
            LIVE_FEATURE_SOURCE_SHA256,
            "3838c14e82ab9e4b6dd319492ccddbdf1224b2e0ceed34d416d3638fe33ba569",
        )

    def test_observer_is_deterministic_and_binds_exact_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = _registry(Path(directory))
            first = _event(
                event_id="event-a",
                selection_id="selection-a",
                decimal_odds="2",
            )
            second = _event(
                event_id="event-b",
                selection_id="selection-b",
                decimal_odds="4",
                exchange_side="lay",
            )
            forward = MirrorSnapshot(revision=3, events=(first, second))
            reversed_snapshot = MirrorSnapshot(
                revision=999,
                events=(second, first),
            )

            observations = observe_registered_strategy_live_features(
                "input-a",
                reversed_snapshot,
                decision_at=DECISION_AT,
                registry=registry,
                model_version_id="model-v1",
            )

            self.assertEqual(
                [observation.quote_key for observation in observations],
                [first.quote_key, second.quote_key],
            )
            self.assertEqual(
                observations[0].feature_hex,
                (1.0 / float(first.decimal_odds)).hex(),
            )
            self.assertEqual(
                observations[1].feature_hex,
                (1.0 / float(second.decimal_odds)).hex(),
            )
            self.assertEqual(
                observations[0].available_at,
                "2026-01-03T00:00:02Z",
            )
            expected_snapshot_sha = market_snapshot_sha256(forward)
            self.assertEqual(
                expected_snapshot_sha,
                market_snapshot_sha256(reversed_snapshot),
                "process-local mirror revision and event order must not change market evidence identity",
            )
            self.assertTrue(
                all(
                    item.market_snapshot_sha256 == expected_snapshot_sha
                    for item in observations
                )
            )
            self.assertNotEqual(
                observations[0].market_event_sha256,
                observations[1].market_event_sha256,
            )
            self.assertNotEqual(
                observations[0].evidence_sha256,
                observations[1].evidence_sha256,
            )
            authority = resolve_registered_live_feature_authority(
                registry,
                "model-v1",
                decision_at=DECISION_AT,
            )
            self.assertTrue(
                all(
                    item.authority_sha256 == authority.authority_sha256
                    for item in observations
                )
            )

    def test_future_local_evidence_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = _registry(Path(directory))
            snapshot = MirrorSnapshot(
                revision=1,
                events=(
                    _event(
                        event_id="event-a",
                        selection_id="selection-a",
                        decimal_odds="2",
                        ingest_ts="2026-01-03T00:00:11Z",
                    ),
                ),
            )
            with self.assertRaisesRegex(
                RegisteredStrategyLiveFeatureError,
                "not causally available",
            ):
                observe_registered_strategy_live_features(
                    "input-a",
                    snapshot,
                    decision_at=DECISION_AT,
                    registry=registry,
                    model_version_id="model-v1",
                )

    def test_future_provider_timestamp_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = _registry(Path(directory))
            snapshot = MirrorSnapshot(
                revision=1,
                events=(
                    _event(
                        event_id="event-a",
                        selection_id="selection-a",
                        decimal_odds="2",
                        source_ts="2026-01-03T00:00:11Z",
                    ),
                ),
            )
            with self.assertRaisesRegex(
                RegisteredStrategyLiveFeatureError,
                "source timestamp is after decision time",
            ):
                observe_registered_strategy_live_features(
                    "input-a",
                    snapshot,
                    decision_at=DECISION_AT,
                    registry=registry,
                    model_version_id="model-v1",
                )

    def test_wrong_registered_feature_definition_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = _registry(
                Path(directory),
                feature_definition_sha256="e" * 64,
            )
            snapshot = MirrorSnapshot(
                revision=1,
                events=(
                    _event(
                        event_id="event-a",
                        selection_id="selection-a",
                        decimal_odds="2",
                    ),
                ),
            )
            with self.assertRaisesRegex(
                RegisteredStrategyLiveFeatureError,
                "does not match the supported live feature contract",
            ):
                observe_registered_strategy_live_features(
                    "input-a",
                    snapshot,
                    decision_at=DECISION_AT,
                    registry=registry,
                    model_version_id="model-v1",
                )

    def test_model_cannot_reference_an_unrelated_feature_set(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = _registry(
                Path(directory),
                model_feature_set_id="other-feature-v1",
            )
            snapshot = MirrorSnapshot(
                revision=1,
                events=(
                    _event(
                        event_id="event-a",
                        selection_id="selection-a",
                        decimal_odds="2",
                    ),
                ),
            )
            with self.assertRaisesRegex(
                RegisteredStrategyLiveFeatureError,
                "not bound to the supported live feature set",
            ):
                observe_registered_strategy_live_features(
                    "input-a",
                    snapshot,
                    decision_at=DECISION_AT,
                    registry=registry,
                    model_version_id="model-v1",
                )

    def test_feature_set_must_precede_model_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = _registry(
                Path(directory),
                feature_available_at="2026-01-02T00:00:01Z",
                model_available_at="2026-01-02T00:00:00Z",
            )
            with self.assertRaisesRegex(
                RegisteredStrategyLiveFeatureError,
                "FeatureSet was not available before ModelVersion",
            ):
                resolve_registered_live_feature_authority(
                    registry,
                    "model-v1",
                    decision_at=DECISION_AT,
                )

    def test_non_open_or_duplicate_quote_snapshot_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = _registry(Path(directory))
            closed = _event(
                event_id="event-a",
                selection_id="selection-a",
                decimal_odds="2",
                status="closed",
            )
            with self.assertRaisesRegex(
                RegisteredStrategyLiveFeatureError,
                "decision-eligible open",
            ):
                observe_registered_strategy_live_features(
                    "input-a",
                    MirrorSnapshot(revision=1, events=(closed,)),
                    decision_at=DECISION_AT,
                    registry=registry,
                    model_version_id="model-v1",
                )

            duplicate = _event(
                event_id="event-b",
                selection_id="selection-b",
                decimal_odds="3",
            )
            with self.assertRaisesRegex(
                RegisteredStrategyLiveFeatureError,
                "duplicate source/quote identity",
            ):
                observe_registered_strategy_live_features(
                    "input-a",
                    MirrorSnapshot(revision=2, events=(duplicate, duplicate)),
                    decision_at=DECISION_AT,
                    registry=registry,
                    model_version_id="model-v1",
                )


if __name__ == "__main__":
    unittest.main()
