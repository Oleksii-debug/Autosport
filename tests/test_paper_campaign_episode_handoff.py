from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
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


    def test_committed_children_exposes_exact_restart_locator_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment, runtime, _finalization = self._terminal_parent(root)
            parent_snapshot = runtime.agent_loop.snapshot()
            parent_checkpoint = runtime.environment.checkpoint()
            handoff = PaperCampaignEpisodeHandoff(runtime)
            policy = self._child_policy(environment)

            with patch(
                "autosport.champion_agent_episode.load_champion_policy",
                return_value=policy,
            ):
                result = self._call(handoff, root, environment, parent_snapshot)

            before = handoff.state_path.read_bytes()
            reopened = PaperCampaignEpisodeHandoff(runtime)
            first = reopened.committed_children()
            second = reopened.committed_children()
            after = handoff.state_path.read_bytes()

            self.assertEqual(first, second)
            self.assertEqual(before, after)
            self.assertEqual(len(first), 1)
            record = first[0]
            child = result.episode
            child_snapshot = child.agent_loop.snapshot()
            child_checkpoint = child.environment.checkpoint()
            self.assertEqual(record.prepare_id, json.loads(before)["handoffs"][parent_checkpoint.checkpoint_id]["prepare_id"])
            self.assertEqual(record.handoff_id, result.receipt.handoff_id)
            self.assertEqual(record.parent_checkpoint_id, parent_checkpoint.checkpoint_id)
            self.assertEqual(
                record.parent_transition_id,
                parent_snapshot.checkpointed_transition_id,
            )
            self.assertEqual(record.parent_episode_id, parent_snapshot.episode_id)
            self.assertEqual(record.parent_policy_id, parent_snapshot.policy_id)
            self.assertEqual(
                record.parent_agent_loop_state_sha256,
                parent_snapshot.state_sha256,
            )
            self.assertEqual(record.environment_id, environment.environment_id)
            self.assertEqual(
                Path(record.child_agent_loop_path),
                (root / "child-agent-loop.json").resolve(strict=False),
            )
            self.assertEqual(record.child_loop_id, "campaign-loop-2")
            self.assertEqual(record.child_episode_key, "campaign-episode-2")
            self.assertEqual(record.canonical_strategy_id, "campaign-champion")
            self.assertEqual(record.config_sha256, parent_snapshot.config_sha256)
            self.assertEqual(
                record.economic_goal_fingerprint,
                parent_snapshot.economic_goal_fingerprint,
            )
            self.assertEqual(record.risk_fingerprint, parent_snapshot.risk_fingerprint)
            self.assertEqual(record.source_sha256, parent_snapshot.source_sha256)
            self.assertEqual(record.admissible_actions, ("PAPER_PROPOSAL",))
            self.assertEqual(record.child_policy_id, child.policy.policy_id)
            self.assertEqual(record.child_episode_id, child_snapshot.episode_id)
            self.assertEqual(
                record.child_initial_checkpoint_id,
                child_checkpoint.checkpoint_id,
            )

    def test_prepared_child_is_not_exposed_as_restart_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment, runtime, _finalization = self._terminal_parent(root)
            parent_snapshot = runtime.agent_loop.snapshot()
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

            before = handoff.state_path.read_bytes()
            self.assertEqual(handoff.committed_children(), ())
            self.assertEqual(handoff.state_path.read_bytes(), before)

    def test_self_consistent_local_handoff_rehash_cannot_forge_restart_locator(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment, runtime, _finalization = self._terminal_parent(root)
            parent_snapshot = runtime.agent_loop.snapshot()
            parent_checkpoint = runtime.environment.checkpoint()
            handoff = PaperCampaignEpisodeHandoff(runtime)
            policy = self._child_policy(environment)

            with patch(
                "autosport.champion_agent_episode.load_champion_policy",
                return_value=policy,
            ):
                self._call(handoff, root, environment, parent_snapshot)

            import autosport.paper_campaign_episode_handoff as handoff_module

            state = json.loads(handoff.state_path.read_text(encoding="utf-8"))
            record = state["handoffs"][parent_checkpoint.checkpoint_id]
            record["child_episode_key"] = "forged-campaign-episode"
            record["prepare_id"] = handoff_module._digest(
                PaperCampaignEpisodeHandoff._prepared_semantic(record)
            )
            record["handoff_id"] = handoff_module._digest(
                {
                    "prepare_id": record["prepare_id"],
                    "child_policy_id": record["child_policy_id"],
                    "child_episode_id": record["child_episode_id"],
                    "child_initial_checkpoint_id": record[
                        "child_initial_checkpoint_id"
                    ],
                }
            )
            bare = {
                "schema": state["schema"],
                "schema_version": state["schema_version"],
                "handoffs": state["handoffs"],
            }
            state["state_sha256"] = handoff_module._digest(bare)
            handoff.state_path.write_text(
                json.dumps(
                    state,
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                    allow_nan=False,
                )
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                PaperCampaignEpisodeHandoffError,
                "independent intent authority",
            ):
                handoff.committed_children()


    def test_child_authority_subclasses_are_rejected_before_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment, runtime, _finalization = self._terminal_parent(root)
            parent_snapshot = runtime.agent_loop.snapshot()
            handoff = PaperCampaignEpisodeHandoff(runtime)

            canonical_registry = ScientificRegistry.initialize_pristine(
                root / "registry.json"
            )
            canonical_artifact_store = FactoryArtifactStore(root / "artifacts")
            canonical_identity = environment.identity
            canonical_actions = frozenset({"PAPER_PROPOSAL"})

            class HostileRegistry(ScientificRegistry):
                armed = False
                field_hook_calls = 0

                def __getattribute__(self, name):
                    if name != "__class__" and type(self).armed:
                        type(self).field_hook_calls += 1
                        raise AssertionError("registry subclass hook executed")
                    return super().__getattribute__(name)

            class HostileArtifactStore(FactoryArtifactStore):
                armed = False
                field_hook_calls = 0

                def __getattribute__(self, name):
                    if name != "__class__" and type(self).armed:
                        type(self).field_hook_calls += 1
                        raise AssertionError("artifact-store subclass hook executed")
                    return super().__getattribute__(name)

            class HostileIdentity(EnvironmentIdentity):
                armed = False
                field_hook_calls = 0

                def __getattribute__(self, name):
                    if name != "__class__" and type(self).armed:
                        type(self).field_hook_calls += 1
                        raise AssertionError("identity subclass hook executed")
                    return super().__getattribute__(name)

            class HostileActions(frozenset):
                hook_calls = 0

                def __len__(self):
                    type(self).hook_calls += 1
                    raise AssertionError("actions length hook executed")

                def __iter__(self):
                    type(self).hook_calls += 1
                    raise AssertionError("actions iteration hook executed")

                def issubset(self, other):
                    del other
                    type(self).hook_calls += 1
                    raise AssertionError("actions subset hook executed")

            hostile_registry = HostileRegistry(root / "registry.json")
            hostile_artifact_store = HostileArtifactStore(root / "artifacts")
            hostile_identity = HostileIdentity(
                source_id=canonical_identity.source_id,
                config_id=canonical_identity.config_id,
                data_id=canonical_identity.data_id,
                protocol_id=canonical_identity.protocol_id,
                cutoff_ts=canonical_identity.cutoff_ts,
                seed=canonical_identity.seed,
            )
            hostile_actions = HostileActions({"PAPER_PROPOSAL"})
            HostileRegistry.armed = True
            HostileArtifactStore.armed = True
            HostileIdentity.armed = True

            def invoke(
                *,
                registry=canonical_registry,
                artifact_store=canonical_artifact_store,
                identity=canonical_identity,
                admissible_actions=canonical_actions,
            ):
                return handoff.start_next_episode(
                    root / "child-agent-loop.json",
                    registry,
                    artifact_store,
                    identity=identity,
                    as_of=_legacy.T4,
                    canonical_strategy_id="campaign-champion",
                    config_sha256=parent_snapshot.config_sha256,
                    episode_key="campaign-episode-2",
                    admissible_actions=admissible_actions,
                    loop_id="campaign-loop-2",
                    economic_goal_fingerprint=(
                        parent_snapshot.economic_goal_fingerprint
                    ),
                    risk_fingerprint=parent_snapshot.risk_fingerprint,
                    source_sha256=parent_snapshot.source_sha256,
                    at=_legacy.T4,
                )

            with self.assertRaisesRegex(TypeError, "exact ScientificRegistry"):
                invoke(registry=hostile_registry)
            self.assertEqual(HostileRegistry.field_hook_calls, 0)

            with self.assertRaisesRegex(TypeError, "exact FactoryArtifactStore"):
                invoke(artifact_store=hostile_artifact_store)
            self.assertEqual(HostileArtifactStore.field_hook_calls, 0)

            with self.assertRaisesRegex(TypeError, "exact EnvironmentIdentity"):
                invoke(identity=hostile_identity)
            self.assertEqual(HostileIdentity.field_hook_calls, 0)

            with self.assertRaisesRegex(
                PaperCampaignEpisodeHandoffError,
                "non-empty exact frozenset",
            ):
                invoke(admissible_actions=hostile_actions)
            self.assertEqual(HostileActions.hook_calls, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
