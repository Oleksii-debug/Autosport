from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from autosport.agent_loop import AgentLoopPhase
from autosport.champion_agent_episode import (
    ChampionAgentEpisode,
    ChampionAgentEpisodeError,
)
from autosport.learning_environment import EnvironmentIdentity
from autosport.paper_campaign_episode_handoff import (
    PaperCampaignEpisodeHandoff,
    PaperCampaignEpisodeHandoffError,
)
from autosport.policy_deployment import ActivationBinding, DeploymentScope
from autosport.scientific_registry import ScientificRegistry
from autosport.strategy_model_factory import FactoryArtifactStore
from autosport.transparent_bandit_policy import BanditPolicyState


_BASE_PATH = Path(__file__).with_name("_paper_campaign_runtime_tests_base.py")
_SPEC = importlib.util.spec_from_file_location(
    "_paper_campaign_runtime_tests_base_episode_handoff",
    _BASE_PATH,
)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover - import guard
    raise RuntimeError("cannot load campaign runtime regression base")
_legacy = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_legacy)

CONFIG_SHA256 = "b" * 64


class PaperCampaignEpisodeHandoffTests(unittest.TestCase):
    @staticmethod
    def _terminal_parent(root: Path):
        (
            leg,
            _book,
            ticket_id,
            _decision,
            environment,
            _baseline,
            _observation,
            bridge,
            runtime,
        ) = _legacy._fixture(root)
        resolutions = _legacy._settle(root, leg, "win")
        bridge.reconcile_after_settlement(
            paper_book_path=root / "paper_book.json",
            resolutions=resolutions,
            settled_ticket_ids=(ticket_id,),
            at=_legacy.T4,
        )
        receipt = runtime.finalize_ticket(ticket_id=ticket_id, at=_legacy.T4)
        return environment, runtime, receipt

    @staticmethod
    def _child_policy(environment) -> BanditPolicyState:
        return BanditPolicyState.initial(
            environment_id=environment.environment_id,
            protocol_id=environment.identity.protocol_id,
            config_sha256=CONFIG_SHA256,
            seed=environment.identity.seed,
            action_types=frozenset({"PAPER_PROPOSAL"}),
        )

    @staticmethod
    def _call(
        handoff: PaperCampaignEpisodeHandoff,
        root: Path,
        environment,
        parent_snapshot,
        **deployment_inputs,
    ):
        return handoff.start_next_episode(
            root / "child-agent-loop.json",
            ScientificRegistry.initialize_pristine(root / "registry.json"),
            FactoryArtifactStore(root / "artifacts"),
            identity=environment.identity,
            as_of=_legacy.T4,
            canonical_strategy_id="campaign-champion",
            config_sha256=parent_snapshot.config_sha256,
            episode_key="campaign-episode-2",
            admissible_actions=frozenset({"PAPER_PROPOSAL"}),
            loop_id="campaign-loop-2",
            economic_goal_fingerprint=parent_snapshot.economic_goal_fingerprint,
            risk_fingerprint=parent_snapshot.risk_fingerprint,
            source_sha256=parent_snapshot.source_sha256,
            at=_legacy.T4,
            **deployment_inputs,
        )

    def test_sealed_campaign_checkpoint_starts_distinct_champion_episode_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment, runtime, finalization = self._terminal_parent(root)
            parent_snapshot = runtime.agent_loop.snapshot()
            parent_checkpoint = runtime.environment.checkpoint()
            self.assertIs(parent_snapshot.phase, AgentLoopPhase.CHECKPOINT)
            self.assertEqual(finalization.checkpoint_id, parent_checkpoint.checkpoint_id)

            handoff = PaperCampaignEpisodeHandoff(runtime)
            policy = self._child_policy(environment)
            with patch(
                "autosport.champion_agent_episode.load_champion_policy",
                return_value=policy,
            ):
                first = self._call(handoff, root, environment, parent_snapshot)
                second = self._call(handoff, root, environment, parent_snapshot)

            child_snapshot = first.episode.agent_loop.snapshot()
            child_checkpoint = first.episode.environment.checkpoint()
            self.assertIs(child_snapshot.phase, AgentLoopPhase.BOOTSTRAP)
            self.assertEqual(child_checkpoint.step_index, 0)
            self.assertIsNone(child_checkpoint.last_transition_id)
            self.assertEqual(
                first.receipt.parent_checkpoint_id,
                parent_checkpoint.checkpoint_id,
            )
            self.assertEqual(
                first.receipt.parent_transition_id,
                parent_snapshot.checkpointed_transition_id,
            )
            self.assertNotEqual(child_snapshot.episode_id, parent_snapshot.episode_id)
            self.assertEqual(
                first.episode.environment.episode.episode_key,
                "campaign-episode-2",
            )
            self.assertEqual(child_snapshot.environment_id, parent_snapshot.environment_id)
            self.assertEqual(
                child_snapshot.economic_goal_fingerprint,
                parent_snapshot.economic_goal_fingerprint,
            )
            self.assertEqual(child_snapshot.risk_fingerprint, parent_snapshot.risk_fingerprint)
            self.assertEqual(child_snapshot.config_sha256, parent_snapshot.config_sha256)
            self.assertEqual(first.receipt, second.receipt)

            state = json.loads(handoff.state_path.read_text(encoding="utf-8"))
            self.assertEqual(len(state["handoffs"]), 1)
            record = state["handoffs"][parent_checkpoint.checkpoint_id]
            self.assertEqual(record["status"], "COMMITTED")
            self.assertEqual(record["handoff_id"], first.receipt.handoff_id)

    def test_restart_after_prepared_witness_converges_to_same_child(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment, runtime, _finalization = self._terminal_parent(root)
            parent_snapshot = runtime.agent_loop.snapshot()
            parent_checkpoint = runtime.environment.checkpoint()
            handoff = PaperCampaignEpisodeHandoff(runtime)

            with patch.object(
                ChampionAgentEpisode,
                "initialize_pristine",
                side_effect=ChampionAgentEpisodeError("injected crash boundary"),
            ):
                with self.assertRaisesRegex(
                    PaperCampaignEpisodeHandoffError,
                    "canonical champion child episode rejected",
                ):
                    self._call(handoff, root, environment, parent_snapshot)

            prepared = json.loads(handoff.state_path.read_text(encoding="utf-8"))
            self.assertEqual(
                prepared["handoffs"][parent_checkpoint.checkpoint_id]["status"],
                "PREPARED",
            )

            policy = self._child_policy(environment)
            with patch(
                "autosport.champion_agent_episode.load_champion_policy",
                return_value=policy,
            ):
                recovered = self._call(handoff, root, environment, parent_snapshot)

            committed = json.loads(handoff.state_path.read_text(encoding="utf-8"))
            record = committed["handoffs"][parent_checkpoint.checkpoint_id]
            self.assertEqual(record["status"], "COMMITTED")
            self.assertEqual(record["handoff_id"], recovered.receipt.handoff_id)
            self.assertNotEqual(
                recovered.episode.agent_loop.snapshot().episode_id,
                parent_snapshot.episode_id,
            )

    def test_restart_after_child_write_before_handoff_commit_converges(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment, runtime, _finalization = self._terminal_parent(root)
            parent_snapshot = runtime.agent_loop.snapshot()
            parent_checkpoint = runtime.environment.checkpoint()
            handoff = PaperCampaignEpisodeHandoff(runtime)
            policy = self._child_policy(environment)
            child_path = root / "child-agent-loop.json"

            original_parent_witness = handoff._parent_witness
            witness_calls = 0

            def crash_after_child_write():
                nonlocal witness_calls
                witness_calls += 1
                if witness_calls == 2:
                    raise PaperCampaignEpisodeHandoffError(
                        "injected post-child/pre-commit crash boundary"
                    )
                return original_parent_witness()

            with (
                patch(
                    "autosport.champion_agent_episode.load_champion_policy",
                    return_value=policy,
                ),
                patch.object(
                    handoff,
                    "_parent_witness",
                    side_effect=crash_after_child_write,
                ),
            ):
                with self.assertRaisesRegex(
                    PaperCampaignEpisodeHandoffError,
                    "post-child/pre-commit crash boundary",
                ):
                    self._call(handoff, root, environment, parent_snapshot)

            self.assertTrue(child_path.is_file())
            child_before_retry = child_path.read_bytes()
            prepared = json.loads(handoff.state_path.read_text(encoding="utf-8"))
            self.assertEqual(
                prepared["handoffs"][parent_checkpoint.checkpoint_id]["status"],
                "PREPARED",
            )

            with patch(
                "autosport.champion_agent_episode.load_champion_policy",
                return_value=policy,
            ):
                recovered = self._call(handoff, root, environment, parent_snapshot)

            self.assertEqual(child_path.read_bytes(), child_before_retry)
            child_snapshot = recovered.episode.agent_loop.snapshot()
            self.assertIs(child_snapshot.phase, AgentLoopPhase.BOOTSTRAP)
            self.assertEqual(child_snapshot.loop_id, "campaign-loop-2")
            committed = json.loads(handoff.state_path.read_text(encoding="utf-8"))
            record = committed["handoffs"][parent_checkpoint.checkpoint_id]
            self.assertEqual(record["status"], "COMMITTED")
            self.assertEqual(record["handoff_id"], recovered.receipt.handoff_id)
            self.assertEqual(
                record["child_initial_checkpoint_id"],
                recovered.receipt.child_initial_checkpoint_id,
            )

    def test_restart_after_prepared_state_before_intent_commit_converges(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment, runtime, _finalization = self._terminal_parent(root)
            parent_snapshot = runtime.agent_loop.snapshot()
            parent_checkpoint = runtime.environment.checkpoint()
            handoff = PaperCampaignEpisodeHandoff(runtime)
            policy = self._child_policy(environment)

            original_write_state = handoff._write_state
            injected = False

            def crash_after_prepared_write(handoffs):
                nonlocal injected
                original_write_state(handoffs)
                record = handoffs.get(parent_checkpoint.checkpoint_id)
                if (
                    not injected
                    and isinstance(record, dict)
                    and record.get("status") == "PREPARED"
                ):
                    injected = True
                    raise PaperCampaignEpisodeHandoffError(
                        "injected prepared-state/pre-intent-commit crash boundary"
                    )

            with (
                patch(
                    "autosport.champion_agent_episode.load_champion_policy",
                    return_value=policy,
                ),
                patch.object(
                    handoff,
                    "_write_state",
                    side_effect=crash_after_prepared_write,
                ),
            ):
                with self.assertRaisesRegex(
                    PaperCampaignEpisodeHandoffError,
                    "prepared-state/pre-intent-commit crash boundary",
                ):
                    self._call(handoff, root, environment, parent_snapshot)

            prepared = json.loads(handoff.state_path.read_text(encoding="utf-8"))
            self.assertEqual(
                prepared["handoffs"][parent_checkpoint.checkpoint_id]["status"],
                "PREPARED",
            )

            with patch(
                "autosport.champion_agent_episode.load_champion_policy",
                return_value=policy,
            ):
                recovered = self._call(handoff, root, environment, parent_snapshot)

            committed = json.loads(handoff.state_path.read_text(encoding="utf-8"))
            record = committed["handoffs"][parent_checkpoint.checkpoint_id]
            self.assertEqual(record["status"], "COMMITTED")
            self.assertEqual(record["handoff_id"], recovered.receipt.handoff_id)


    def test_restart_after_committed_state_before_consumption_commit_converges(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment, runtime, _finalization = self._terminal_parent(root)
            parent_snapshot = runtime.agent_loop.snapshot()
            parent_checkpoint = runtime.environment.checkpoint()
            handoff = PaperCampaignEpisodeHandoff(runtime)
            policy = self._child_policy(environment)
            child_path = root / "child-agent-loop.json"

            original_write_state = handoff._write_state
            injected = False

            def crash_after_committed_write(handoffs):
                nonlocal injected
                original_write_state(handoffs)
                record = handoffs.get(parent_checkpoint.checkpoint_id)
                if (
                    not injected
                    and isinstance(record, dict)
                    and record.get("status") == "COMMITTED"
                ):
                    injected = True
                    raise PaperCampaignEpisodeHandoffError(
                        "injected committed-state/pre-consumption-commit crash boundary"
                    )

            with (
                patch(
                    "autosport.champion_agent_episode.load_champion_policy",
                    return_value=policy,
                ),
                patch.object(
                    handoff,
                    "_write_state",
                    side_effect=crash_after_committed_write,
                ),
            ):
                with self.assertRaisesRegex(
                    PaperCampaignEpisodeHandoffError,
                    "committed-state/pre-consumption-commit crash boundary",
                ):
                    self._call(handoff, root, environment, parent_snapshot)

            self.assertTrue(child_path.is_file())
            child_before_retry = child_path.read_bytes()
            committed_before_retry = json.loads(
                handoff.state_path.read_text(encoding="utf-8")
            )
            record_before_retry = committed_before_retry["handoffs"][
                parent_checkpoint.checkpoint_id
            ]
            self.assertEqual(record_before_retry["status"], "COMMITTED")

            with patch(
                "autosport.champion_agent_episode.load_champion_policy",
                return_value=policy,
            ):
                recovered = self._call(handoff, root, environment, parent_snapshot)

            self.assertEqual(child_path.read_bytes(), child_before_retry)
            committed = json.loads(handoff.state_path.read_text(encoding="utf-8"))
            record = committed["handoffs"][parent_checkpoint.checkpoint_id]
            self.assertEqual(record["status"], "COMMITTED")
            self.assertEqual(record["handoff_id"], recovered.receipt.handoff_id)
            self.assertEqual(
                record_before_retry["handoff_id"],
                recovered.receipt.handoff_id,
            )

    def test_unfinished_parent_and_fingerprint_drift_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (
                _leg,
                _book,
                _ticket_id,
                _decision,
                environment,
                _baseline,
                _observation,
                _bridge,
                runtime,
            ) = _legacy._fixture(root)
            handoff = PaperCampaignEpisodeHandoff(runtime)
            parent_snapshot = runtime.agent_loop.snapshot()
            with self.assertRaisesRegex(
                PaperCampaignEpisodeHandoffError,
                "fully committed parent CHECKPOINT",
            ):
                self._call(handoff, root, environment, parent_snapshot)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment, runtime, _finalization = self._terminal_parent(root)
            handoff = PaperCampaignEpisodeHandoff(runtime)
            parent_snapshot = runtime.agent_loop.snapshot()
            with self.assertRaisesRegex(
                PaperCampaignEpisodeHandoffError,
                "config fingerprint differs",
            ):
                handoff.start_next_episode(
                    root / "child-agent-loop.json",
                    ScientificRegistry.initialize_pristine(root / "registry.json"),
                    FactoryArtifactStore(root / "artifacts"),
                    identity=environment.identity,
                    as_of=_legacy.T4,
                    canonical_strategy_id="campaign-champion",
                    config_sha256="d" * 64,
                    episode_key="campaign-episode-2",
                    admissible_actions=frozenset({"PAPER_PROPOSAL"}),
                    loop_id="campaign-loop-2",
                    economic_goal_fingerprint=parent_snapshot.economic_goal_fingerprint,
                    risk_fingerprint=parent_snapshot.risk_fingerprint,
                    source_sha256=parent_snapshot.source_sha256,
                    at=_legacy.T4,
                )


    def test_deployment_bound_parent_preserves_generation_at_episode_boundary(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment, runtime, _finalization = self._terminal_parent(root)
            parent_snapshot = runtime.agent_loop.snapshot()
            training = EnvironmentIdentity(
                source_id=environment.identity.source_id,
                config_id=environment.identity.config_id,
                data_id="campaign-training-data",
                protocol_id=environment.identity.protocol_id,
                cutoff_ts="2026-09-20T02:59:00Z",
                seed=environment.identity.seed,
            )
            policy = BanditPolicyState.initial(
                environment_id=training.environment_id,
                protocol_id=training.protocol_id,
                config_sha256=parent_snapshot.config_sha256,
                seed=training.seed,
                action_types=frozenset({"PAPER_PROPOSAL"}),
            )
            scope = DeploymentScope(
                canonical_strategy_id="campaign-champion",
                sport_domain="table_tennis",
                competition_scope="campaign-fixture",
                market_semantics_id="winner-v1",
                provider_source_class="paper",
                feature_schema_id="campaign-feature-v1",
                protocol_id=training.protocol_id,
                action_semantics_id="paper-proposal-v1",
                reward_definition_id="paper-net-reward-v1",
                config_sha256=parent_snapshot.config_sha256,
            )
            binding = ActivationBinding(
                policy_id=policy.policy_id,
                policy_artifact_sha256="1" * 64,
                training_environment_id=training.environment_id,
                training_data_id=training.data_id,
                training_dataset_record_sha256="2" * 64,
                training_cutoff_ts=training.cutoff_ts,
                promotion_decision_id="campaign-promotion",
                promotion_decision_record_sha256="3" * 64,
                promotion_evidence_id="4" * 64,
                promotion_evidence_record_sha256="5" * 64,
                evaluation_bundle_id="campaign-evaluation",
                evaluation_bundle_record_sha256="6" * 64,
                deployment_scope_id=scope.scope_id,
                deployment_environment_id=environment.environment_id,
                deployment_data_id=environment.identity.data_id,
                deployment_dataset_record_sha256="7" * 64,
                deployment_cutoff_ts=environment.identity.cutoff_ts,
                snapshot_available_at=_legacy.T2,
                activation_at=_legacy.T2,
                admissible_actions=("PAPER_PROPOSAL",),
                economic_goal_fingerprint=parent_snapshot.economic_goal_fingerprint,
                risk_fingerprint=parent_snapshot.risk_fingerprint,
                dataset_lineage_proof_sha256="8" * 64,
            )
            bound_parent = replace(
                parent_snapshot,
                activation_binding_id=binding.binding_id,
            )
            authority = SimpleNamespace(
                scope=scope,
                binding=binding,
                training_identity=training,
                deployment_identity=environment.identity,
            )
            handoff = PaperCampaignEpisodeHandoff(runtime)

            with patch.object(
                runtime.agent_loop,
                "snapshot",
                return_value=bound_parent,
            ):
                with self.assertRaisesRegex(
                    PaperCampaignEpisodeHandoffError,
                    "requires canonical deployment resolver inputs",
                ):
                    self._call(
                        handoff,
                        root,
                        environment,
                        bound_parent,
                    )

            semantic_inputs = object()
            market_store = object()
            runtime_store = object()
            with (
                patch.object(
                    runtime.agent_loop,
                    "snapshot",
                    return_value=bound_parent,
                ),
                patch(
                    "autosport.paper_campaign_episode_handoff.load_deployment_authority",
                    return_value=authority,
                ) as load_authority,
                patch(
                    "autosport.champion_agent_episode._require_canonical_inputs",
                    return_value=(semantic_inputs, market_store, runtime_store),
                ),
                patch(
                    "autosport.champion_agent_episode.load_champion_policy",
                    return_value=policy,
                ) as load_policy,
                patch(
                    "autosport.champion_agent_episode.validate_canonical_activation_binding",
                    return_value=SimpleNamespace(deployment_scope=scope),
                ),
            ):
                result = self._call(
                    handoff,
                    root,
                    environment,
                    bound_parent,
                    semantic_inputs=semantic_inputs,
                    market_store=market_store,
                    runtime_authority_store=runtime_store,
                )

            load_authority.assert_called_once_with(
                runtime.agent_loop.path,
                expected_binding_id=binding.binding_id,
            )
            assert load_policy.call_args.kwargs["as_of"] == binding.activation_at
            assert result.episode.activation_binding == binding
            assert (
                result.episode.agent_loop.snapshot().activation_binding_id
                == binding.binding_id
            )
            assert (
                result.episode.agent_loop.snapshot().activation_binding_id
                == bound_parent.activation_binding_id
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
