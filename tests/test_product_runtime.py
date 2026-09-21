from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autosport.causal_collector import (
    CollectorDelta,
    DesktopDeltaCheckpointStore,
    canonical_event_digest,
    digest_source_payload,
)
from autosport.domain import MarketEvent
from autosport.event_lifecycle import CatalogPage
from autosport.ingestion_health import SourceHealthStore
from autosport.learning_environment import EnvironmentIdentity
from autosport.market_bus import MarketEventBus
from autosport.policy_deployment import (
    ActivationBinding,
    DeploymentAuthority,
    DeploymentScope,
)
from autosport.product_runtime import (
    ProductCompositionError,
    build_autonomous_product_runtime,
)


class _Clock:
    def __init__(self) -> None:
        self.value = "2026-09-20T13:58:00+00:00"

    def __call__(self) -> str:
        return self.value


class _Source:
    def __init__(
        self,
        source_id: str = "provider-a",
        *,
        resolved_event: MarketEvent | None = None,
    ) -> None:
        self.source_id = source_id
        self.stream_epoch = "epoch-1"
        self.resolved_event = resolved_event

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
        if self.resolved_event is None:
            raise AssertionError("no market delta should be resolved in this test")
        if delta.event_id != self.resolved_event.event_id:
            raise AssertionError("unexpected collector delta event")
        return self.resolved_event


class _CrashAfterPersistBus(MarketEventBus):
    """Simulate process loss after SQLite/subscriber delivery but before app progress."""

    crashed = False

    def publish(self, event):
        accepted = super().publish(event)
        if not type(self).crashed:
            type(self).crashed = True
            raise RuntimeError("crash-after-market-persist")
        return accepted


def _event() -> MarketEvent:
    return MarketEvent.from_dict(
        {
            "event_id": "event-1",
            "market_id": "winner",
            "selection_id": "player-a",
            "decimal_odds": "1.80",
            "observed_ts": "2026-09-20T13:57:55+00:00",
            "source_id": "provider-a",
            "sequence": 1,
            "market_type": "winner",
            "status": "open",
            "source_ts": "2026-09-20T13:57:54+00:00",
            "ingest_ts": "2026-09-20T13:57:56+00:00",
            "metadata": {},
            "score_state": None,
        }
    )


def _delta(event: MarketEvent) -> CollectorDelta:
    return CollectorDelta(
        schema_version=1,
        delta_id="delta-1",
        source_id=event.source_id,
        lawful_terms_ref="terms:provider-a:v1",
        retention_ref="retention:provider-a:v1",
        stream_epoch="epoch-1",
        source_cursor="1",
        cursor_position=1,
        event_dedupe_key=event.dedupe_key,
        event_id=event.event_id,
        source_payload_digest=digest_source_payload('{"provider":"payload"}'),
        canonical_event_digest=canonical_event_digest(event),
        source_observed_at="2026-09-20T13:57:55+00:00",
        collector_received_at="2026-09-20T13:57:56+00:00",
        collector_committed_at="2026-09-20T13:57:57+00:00",
        desktop_available_at="2026-09-20T13:57:58+00:00",
    )


def _deployment_authority(*, policy_id: str = "c" * 64) -> DeploymentAuthority:
    scope = DeploymentScope(
        canonical_strategy_id="strategy-a",
        sport_domain="football",
        competition_scope="league-a",
        market_semantics_id="winner-v1",
        provider_source_class="official-api",
        feature_schema_id="features-v1",
        protocol_id="protocol-v1",
        action_semantics_id="actions-v1",
        reward_definition_id="reward-v1",
        config_sha256="1" * 64,
    )
    training_identity = EnvironmentIdentity(
        source_id="provider-a",
        config_id="training-config",
        data_id="training-data",
        protocol_id="protocol-v1",
        cutoff_ts="2026-09-18T00:00:00Z",
        seed=1,
    )
    deployment_identity = EnvironmentIdentity(
        source_id="provider-a",
        config_id="deployment-config",
        data_id="deployment-data",
        protocol_id="protocol-v1",
        cutoff_ts="2026-09-20T00:00:00Z",
        seed=2,
    )
    binding = ActivationBinding(
        policy_id=policy_id,
        policy_artifact_sha256="2" * 64,
        training_environment_id=training_identity.environment_id,
        training_data_id="training-data",
        training_dataset_record_sha256="3" * 64,
        training_cutoff_ts=training_identity.cutoff_ts,
        promotion_decision_id="promotion-1",
        promotion_decision_record_sha256="4" * 64,
        promotion_evidence_id="5" * 64,
        promotion_evidence_record_sha256="6" * 64,
        evaluation_bundle_id="evaluation-1",
        evaluation_bundle_record_sha256="7" * 64,
        deployment_scope_id=scope.scope_id,
        deployment_environment_id=deployment_identity.environment_id,
        deployment_data_id="deployment-data",
        deployment_dataset_record_sha256="8" * 64,
        dataset_lineage_proof_sha256="b" * 64,
        deployment_cutoff_ts=deployment_identity.cutoff_ts,
        snapshot_available_at="2026-09-20T00:00:00Z",
        activation_at="2026-09-20T00:00:01Z",
        admissible_actions=("WAIT",),
        economic_goal_fingerprint="9" * 64,
        risk_fingerprint="a" * 64,
    )
    return DeploymentAuthority(
        scope=scope,
        binding=binding,
        training_identity=training_identity,
        deployment_identity=deployment_identity,
    )


class AutonomousProductCompositionTests(unittest.TestCase):
    def test_campaign_manifest_binds_exact_deployment_authority_on_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            authority = _deployment_authority()
            runtime = build_autonomous_product_runtime(
                workspace=root,
                source=_Source(),
                clock=_Clock(),
                sleep=lambda _: None,
                initial_bankroll="100",
                deployment_authority=authority,
                campaign_episode_key="paper-campaign-001",
            )
            runtime.close()

            raw = json.loads((root / "product_composition.json").read_text(encoding="utf-8"))
            self.assertEqual(raw["schema_version"], 3)
            self.assertEqual(
                raw["deployment_authority_sha256"],
                authority.authority_sha256,
            )
            self.assertEqual(
                raw["activation_binding_id"],
                authority.binding.binding_id,
            )
            self.assertEqual(raw["policy_id"], authority.binding.policy_id)
            self.assertEqual(
                raw["deployment_environment_id"],
                authority.deployment_identity.environment_id,
            )
            self.assertEqual(raw["campaign_episode_key"], "paper-campaign-001")

            restored = build_autonomous_product_runtime(
                workspace=root,
                source=_Source(),
                clock=_Clock(),
                sleep=lambda _: None,
                initial_bankroll="100",
                deployment_authority=authority,
                campaign_episode_key="paper-campaign-001",
            )
            try:
                self.assertEqual(
                    restored.manifest.deployment_authority_sha256,
                    authority.authority_sha256,
                )
                self.assertEqual(
                    restored.manifest.activation_binding_id,
                    authority.binding.binding_id,
                )
                self.assertEqual(restored.manifest.policy_id, authority.binding.policy_id)
                self.assertEqual(
                    restored.manifest.deployment_environment_id,
                    authority.deployment_identity.environment_id,
                )
                self.assertEqual(
                    restored.manifest.campaign_episode_key,
                    "paper-campaign-001",
                )
            finally:
                restored.close()

    def test_campaign_manifest_rejects_partial_identity_before_publication(self) -> None:
        authority = _deployment_authority()
        cases = (
            {"deployment_authority": authority, "campaign_episode_key": None},
            {"deployment_authority": None, "campaign_episode_key": "paper-campaign-001"},
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, case in enumerate(cases):
                workspace = root / f"case-{index}"
                with self.subTest(index=index):
                    with self.assertRaisesRegex(
                        ProductCompositionError,
                        "campaign composition identity fields must be provided together",
                    ):
                        build_autonomous_product_runtime(
                            workspace=workspace,
                            source=_Source(),
                            clock=_Clock(),
                            sleep=lambda _: None,
                            initial_bankroll="100",
                            **case,
                        )
                    self.assertFalse((workspace / "product_composition.json").exists())

    def test_campaign_restart_with_different_deployment_authority_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            authority = _deployment_authority()
            runtime = build_autonomous_product_runtime(
                workspace=root,
                source=_Source(),
                clock=_Clock(),
                sleep=lambda _: None,
                initial_bankroll="100",
                deployment_authority=authority,
                campaign_episode_key="paper-campaign-001",
            )
            runtime.close()

            with self.assertRaisesRegex(
                ProductCompositionError,
                "conflicts with durable product composition",
            ):
                build_autonomous_product_runtime(
                    workspace=root,
                    source=_Source(),
                    clock=_Clock(),
                    sleep=lambda _: None,
                    initial_bankroll="100",
                    deployment_authority=_deployment_authority(policy_id="d" * 64),
                    campaign_episode_key="paper-campaign-001",
                )

    def test_clean_workspace_builds_and_restart_restores_same_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            runtime = build_autonomous_product_runtime(
                workspace=root,
                source=_Source(),
                clock=clock,
                sleep=lambda _: None,
                initial_bankroll="100",
            )
            try:
                first_status = runtime.status()
                self.assertEqual(first_status.cycles_completed, 0)
                self.assertEqual(first_status.source_id, "provider-a")
                session_id = first_status.session_id

                result = runtime.tick()
                self.assertEqual(result.cycle_index, 1)
                self.assertEqual(runtime.status().cycles_completed, 1)
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
                restored_status = restored.status()
                self.assertEqual(restored_status.session_id, session_id)
                self.assertEqual(restored_status.cycles_completed, 1)
                self.assertEqual(restored.manifest.source_id, "provider-a")
                self.assertEqual(restored.manifest.initial_bankroll, "100")
            finally:
                restored.close()

    def test_restart_with_different_source_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = build_autonomous_product_runtime(
                workspace=root,
                source=_Source("provider-a"),
                clock=_Clock(),
                sleep=lambda _: None,
                initial_bankroll="100",
            )
            runtime.close()

            with self.assertRaisesRegex(
                ProductCompositionError,
                "source_id conflicts with durable product composition",
            ):
                build_autonomous_product_runtime(
                    workspace=root,
                    source=_Source("provider-b"),
                    clock=_Clock(),
                    sleep=lambda _: None,
                    initial_bankroll="100",
                )

    def test_restart_with_changed_initial_bankroll_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = build_autonomous_product_runtime(
                workspace=root,
                source=_Source(),
                clock=_Clock(),
                sleep=lambda _: None,
                initial_bankroll="100",
            )
            runtime.close()

            with self.assertRaisesRegex(
                ProductCompositionError,
                "initial_bankroll conflicts with durable product composition",
            ):
                build_autonomous_product_runtime(
                    workspace=root,
                    source=_Source(),
                    clock=_Clock(),
                    sleep=lambda _: None,
                    initial_bankroll="101",
                )

    def test_invalid_initial_bankroll_does_not_publish_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(
                ValueError,
                "initial_bankroll must construct a valid PaperBook",
            ):
                build_autonomous_product_runtime(
                    workspace=root,
                    source=_Source(),
                    clock=_Clock(),
                    sleep=lambda _: None,
                    initial_bankroll="not-a-number",
                )
            self.assertFalse((root / "product_composition.json").exists())

    def test_corrupt_manifest_fails_closed_before_runtime_construction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.mkdir(parents=True, exist_ok=True)
            (root / "product_composition.json").write_text(
                '{"schema":"autosport.autonomous_product_composition"}',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ProductCompositionError,
                "manifest schema mismatch",
            ):
                build_autonomous_product_runtime(
                    workspace=root,
                    source=_Source(),
                    clock=_Clock(),
                    sleep=lambda _: None,
                    initial_bankroll="100",
                )

    def test_crash_after_market_persist_replays_canonical_application_exactly_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            event = _event()
            delta = _delta(event)
            source = _Source(resolved_event=event)
            _CrashAfterPersistBus.crashed = False

            with patch(
                "autosport.product_runtime.MarketEventBus",
                _CrashAfterPersistBus,
            ):
                runtime = build_autonomous_product_runtime(
                    workspace=root,
                    source=source,
                    clock=clock,
                    sleep=lambda _: None,
                    initial_bankroll="100",
                )
                try:
                    self.assertTrue(runtime.collector.delta_store.append(delta))
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "crash-after-market-persist",
                    ):
                        runtime.coordinator.desktop_consumer.drain(
                            as_of=clock.value,
                        )
                    self.assertEqual(len(runtime.market_store.events(event.event_id)), 1)
                    self.assertEqual(
                        SourceHealthStore(root / "source_health.json")
                        .get(source.source_id)
                        .poll_count,
                        0,
                    )
                finally:
                    runtime.close()

            restored = build_autonomous_product_runtime(
                workspace=root,
                source=source,
                clock=clock,
                sleep=lambda _: None,
                initial_bankroll="100",
            )
            try:
                self.assertEqual(
                    restored.coordinator.desktop_consumer.drain(as_of=clock.value),
                    (delta.delta_id,),
                )
                self.assertEqual(len(restored.market_store.events(event.event_id)), 1)
                health = SourceHealthStore(root / "source_health.json").get(source.source_id)
                self.assertEqual(health.poll_count, 1)
                self.assertEqual(health.total_received, 1)
                self.assertEqual(health.total_accepted, 1)
                self.assertEqual(health.last_cursor, delta.source_cursor)

                receipt = DesktopDeltaCheckpointStore(
                    root / "desktop_acks.json"
                ).application_receipt(delta)
                self.assertIsNotNone(receipt)
                self.assertTrue(receipt.receipt_id.startswith("canonical-desktop:"))

                self.assertEqual(
                    restored.coordinator.desktop_consumer.drain(as_of=clock.value),
                    (),
                )
                self.assertEqual(
                    SourceHealthStore(root / "source_health.json")
                    .get(source.source_id)
                    .poll_count,
                    1,
                )
            finally:
                restored.close()


if __name__ == "__main__":
    unittest.main()
