from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from decimal import Decimal
from unittest.mock import patch
from pathlib import Path

import autosport.registered_strategy_live_feature as live_feature_module
from autosport.domain import MarketEvent
from autosport.market_mirror import MirrorSnapshot
from autosport.registered_strategy_live_feature import (
    LIVE_FEATURE_DEFINITION_JSON,
    LIVE_FEATURE_DEFINITION_SHA256,
    LIVE_FEATURE_FRESHNESS_POLICY_ID,
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
FRESHNESS_MAX_AGE_SECONDS = 10


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
            "d24b53c347373bc0f6cfbc32ee2b19755505a1c21799728c1a7229b4413ab76b",
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
            "9fe83ff72351f461ef20cc53f6415248d414b5cf1aee1f844886c11e32d9d734",
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
                freshness_max_age_seconds=FRESHNESS_MAX_AGE_SECONDS,
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
            self.assertEqual(observations[0].decision_at, DECISION_AT)
            self.assertEqual(
                observations[0].freshness_policy_id,
                LIVE_FEATURE_FRESHNESS_POLICY_ID,
            )
            self.assertEqual(
                observations[0].freshness_max_age_seconds,
                FRESHNESS_MAX_AGE_SECONDS,
            )
            self.assertEqual(
                observations[0].freshness_timestamp,
                "2026-01-03T00:00:00Z",
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
                    freshness_max_age_seconds=FRESHNESS_MAX_AGE_SECONDS,
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
                    freshness_max_age_seconds=FRESHNESS_MAX_AGE_SECONDS,
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
                    freshness_max_age_seconds=FRESHNESS_MAX_AGE_SECONDS,
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
                    freshness_max_age_seconds=FRESHNESS_MAX_AGE_SECONDS,
                    registry=registry,
                    model_version_id="model-v1",
                )

    def test_canonical_feature_set_payload_schema_resolves_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = _registry(Path(directory))
            entry = registry.get("FeatureSet", LIVE_FEATURE_SET_ID)
            self.assertIsNotNone(entry)
            assert entry is not None
            self.assertEqual(
                set(entry.payload),
                {
                    "feature_set_id",
                    "version",
                    "definition_sha256",
                    "source_sha256",
                    "available_at",
                },
            )
            self.assertNotIn("available_at_utc", entry.payload)

            authority = resolve_registered_live_feature_authority(
                registry,
                "model-v1",
                decision_at=DECISION_AT,
            )

            self.assertEqual(authority.feature_set_id, LIVE_FEATURE_SET_ID)
            self.assertEqual(authority.feature_available_at, FEATURE_AVAILABLE_AT)
            self.assertEqual(
                authority.feature_definition_sha256,
                LIVE_FEATURE_DEFINITION_SHA256,
            )
            self.assertEqual(
                authority.feature_source_sha256,
                LIVE_FEATURE_SOURCE_SHA256,
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

    def test_backdated_feature_set_appended_after_model_fails_causal_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = ScientificRegistry.initialize_pristine(
                Path(directory) / "scientific_registry.json"
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
                    created_at=MODEL_AVAILABLE_AT,
                )
            )
            registry.append(
                FeatureSet(
                    feature_set_id=LIVE_FEATURE_SET_ID,
                    version=LIVE_FEATURE_SET_VERSION,
                    definition_sha256=LIVE_FEATURE_DEFINITION_SHA256,
                    source_sha256=LIVE_FEATURE_SOURCE_SHA256,
                    available_at_utc=FEATURE_AVAILABLE_AT,
                )
            )

            with self.assertRaisesRegex(
                RegisteredStrategyLiveFeatureError,
                "does not durably precede ModelVersion",
            ):
                resolve_registered_live_feature_authority(
                    registry,
                    "model-v1",
                    decision_at=DECISION_AT,
                )

    def test_stale_open_event_fails_explicit_live_freshness_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = _registry(Path(directory))
            stale = _event(
                event_id="event-a",
                selection_id="selection-a",
                decimal_odds="2",
                observed_ts="2026-01-02T23:59:00Z",
                ingest_ts="2026-01-02T23:59:01Z",
                source_ts="2026-01-02T23:58:59Z",
            )
            with self.assertRaisesRegex(
                RegisteredStrategyLiveFeatureError,
                "outside the live freshness boundary",
            ):
                observe_registered_strategy_live_features(
                    "input-a",
                    MirrorSnapshot(revision=1, events=(stale,)),
                    decision_at=DECISION_AT,
                    freshness_max_age_seconds=FRESHNESS_MAX_AGE_SECONDS,
                    registry=registry,
                    model_version_id="model-v1",
                )

    def test_exact_freshness_boundary_is_inclusive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = _registry(Path(directory))
            event = _event(
                event_id="event-a",
                selection_id="selection-a",
                decimal_odds="2",
                source_ts="2026-01-03T00:00:00Z",
            )
            (observation,) = observe_registered_strategy_live_features(
                "input-a",
                MirrorSnapshot(revision=1, events=(event,)),
                decision_at=DECISION_AT,
                freshness_max_age_seconds=FRESHNESS_MAX_AGE_SECONDS,
                registry=registry,
                model_version_id="model-v1",
            )
            self.assertEqual(observation.freshness_timestamp, "2026-01-03T00:00:00Z")
            self.assertEqual(
                observation.freshness_max_age_seconds,
                FRESHNESS_MAX_AGE_SECONDS,
            )

    def test_decision_and_freshness_policy_are_evidence_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = _registry(Path(directory))
            event = _event(
                event_id="event-a",
                selection_id="selection-a",
                decimal_odds="2",
            )
            snapshot = MirrorSnapshot(revision=1, events=(event,))
            (baseline,) = observe_registered_strategy_live_features(
                "input-a",
                snapshot,
                decision_at=DECISION_AT,
                freshness_max_age_seconds=10,
                registry=registry,
                model_version_id="model-v1",
            )
            (different_decision,) = observe_registered_strategy_live_features(
                "input-a",
                snapshot,
                decision_at="2026-01-03T00:00:09Z",
                freshness_max_age_seconds=10,
                registry=registry,
                model_version_id="model-v1",
            )
            (different_policy,) = observe_registered_strategy_live_features(
                "input-a",
                snapshot,
                decision_at=DECISION_AT,
                freshness_max_age_seconds=11,
                registry=registry,
                model_version_id="model-v1",
            )

            for other in (different_decision, different_policy):
                self.assertEqual(other.market_event_sha256, baseline.market_event_sha256)
                self.assertEqual(other.market_snapshot_sha256, baseline.market_snapshot_sha256)
                self.assertEqual(other.authority_sha256, baseline.authority_sha256)
                self.assertNotEqual(other.evidence_sha256, baseline.evidence_sha256)
            self.assertNotEqual(different_decision.decision_at, baseline.decision_at)
            self.assertNotEqual(
                different_policy.freshness_max_age_seconds,
                baseline.freshness_max_age_seconds,
            )

    def test_registry_instance_get_shadow_cannot_replace_durable_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = _registry(Path(directory))
            registry.get = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
            authority = resolve_registered_live_feature_authority(
                registry,
                "model-v1",
                decision_at=DECISION_AT,
            )
            self.assertEqual(authority.model_version_id, "model-v1")
            self.assertEqual(authority.feature_set_id, LIVE_FEATURE_SET_ID)

    def test_registry_module_binding_rebind_cannot_replace_captured_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = _registry(Path(directory))
            attacker_called = False

            class AttackerRegistry:
                def __init__(self, path: object) -> None:
                    nonlocal attacker_called
                    attacker_called = True
                    raise AssertionError("attacker registry type executed")

            with patch.object(
                live_feature_module,
                "ScientificRegistry",
                AttackerRegistry,
            ):
                with self.assertRaisesRegex(
                    RegisteredStrategyLiveFeatureError,
                    "ScientificRegistry type changed",
                ):
                    resolve_registered_live_feature_authority(
                        registry,
                        "model-v1",
                        decision_at=DECISION_AT,
                    )

            self.assertFalse(attacker_called)

    def test_registry_constructor_rebind_cannot_redirect_durable_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = _registry(Path(directory))
            attacker_called = False

            def attacker_init(self: ScientificRegistry, path: object) -> None:
                nonlocal attacker_called
                attacker_called = True
                raise AssertionError("attacker registry constructor executed")

            with patch.object(ScientificRegistry, "__init__", attacker_init):
                with self.assertRaisesRegex(
                    RegisteredStrategyLiveFeatureError,
                    "ScientificRegistry read/causal authority changed",
                ):
                    resolve_registered_live_feature_authority(
                        registry,
                        "model-v1",
                        decision_at=DECISION_AT,
                    )

            self.assertFalse(attacker_called)

    def test_model_version_validation_rebind_cannot_rewrite_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = _registry(Path(directory))
            attacker_called = False

            def attacker_post_init(self: ModelVersion) -> None:
                nonlocal attacker_called
                attacker_called = True

            with patch.object(ModelVersion, "__post_init__", attacker_post_init):
                with self.assertRaisesRegex(
                    RegisteredStrategyLiveFeatureError,
                    "scientific record validation changed",
                ):
                    resolve_registered_live_feature_authority(
                        registry,
                        "model-v1",
                        decision_at=DECISION_AT,
                    )

            self.assertFalse(attacker_called)

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
                    freshness_max_age_seconds=FRESHNESS_MAX_AGE_SECONDS,
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
                    freshness_max_age_seconds=FRESHNESS_MAX_AGE_SECONDS,
                    registry=registry,
                    model_version_id="model-v1",
                )


if __name__ == "__main__":
    unittest.main()
