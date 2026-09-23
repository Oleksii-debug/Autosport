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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
