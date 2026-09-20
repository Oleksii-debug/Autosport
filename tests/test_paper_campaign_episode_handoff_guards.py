from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autosport.champion_agent_episode import ChampionAgentEpisode
from autosport.paper_campaign_episode_handoff import (
    PaperCampaignEpisodeHandoff,
    PaperCampaignEpisodeHandoffError,
)
from autosport.scientific_registry import ScientificRegistry
from autosport.strategy_model_factory import FactoryArtifactStore
from autosport.transparent_bandit_policy import BanditPolicyState


_BASE_PATH = Path(__file__).with_name("_paper_campaign_runtime_tests_base.py")
_SPEC = importlib.util.spec_from_file_location(
    "_paper_campaign_runtime_tests_base_episode_handoff_guards",
    _BASE_PATH,
)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover - import guard
    raise RuntimeError("cannot load campaign runtime regression base")
_legacy = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_legacy)

CONFIG_SHA256 = "b" * 64


class PaperCampaignEpisodeHandoffGuardTests(unittest.TestCase):
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
        runtime.finalize_ticket(ticket_id=ticket_id, at=_legacy.T4)
        return environment, runtime

    @staticmethod
    def _policy(environment) -> BanditPolicyState:
        return BanditPolicyState.initial(
            environment_id=environment.environment_id,
            protocol_id=environment.identity.protocol_id,
            config_sha256=CONFIG_SHA256,
            seed=environment.identity.seed,
            action_types=frozenset({"PAPER_PROPOSAL"}),
        )

    @staticmethod
    def _start(
        handoff: PaperCampaignEpisodeHandoff,
        root: Path,
        environment,
        parent_snapshot,
        *,
        child_name: str = "child-agent-loop.json",
        episode_key: str = "campaign-episode-2",
        loop_id: str = "campaign-loop-2",
        at: str = _legacy.T4,
    ):
        return handoff.start_next_episode(
            root / child_name,
            ScientificRegistry.initialize_pristine(root / "registry.json"),
            FactoryArtifactStore(root / "artifacts"),
            identity=environment.identity,
            as_of=_legacy.T4,
            canonical_strategy_id="campaign-champion",
            config_sha256=parent_snapshot.config_sha256,
            episode_key=episode_key,
            admissible_actions=frozenset({"PAPER_PROPOSAL"}),
            loop_id=loop_id,
            economic_goal_fingerprint=parent_snapshot.economic_goal_fingerprint,
            risk_fingerprint=parent_snapshot.risk_fingerprint,
            source_sha256=parent_snapshot.source_sha256,
            at=at,
        )

    def test_child_episode_cannot_be_backdated_before_parent_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "workspace"
            root.mkdir()
            with patch.dict(
                os.environ,
                {"AUTOSPORT_MONOTONIC_AUTHORITY_ROOT": str(base / "authority")},
            ):
                environment, runtime = self._terminal_parent(root)
                snapshot = runtime.agent_loop.snapshot()
                handoff = PaperCampaignEpisodeHandoff(runtime)
                with self.assertRaisesRegex(
                    PaperCampaignEpisodeHandoffError,
                    "cannot begin before the sealed parent CHECKPOINT",
                ):
                    self._start(
                        handoff,
                        root,
                        environment,
                        snapshot,
                        at=_legacy.T3,
                    )

    def test_prechild_intent_survives_sidecar_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "workspace"
            root.mkdir()
            with patch.dict(
                os.environ,
                {"AUTOSPORT_MONOTONIC_AUTHORITY_ROOT": str(base / "authority")},
            ):
                environment, runtime = self._terminal_parent(root)
                snapshot = runtime.agent_loop.snapshot()
                policy = self._policy(environment)
                handoff = PaperCampaignEpisodeHandoff(runtime)
                created: dict[str, str] = {}
                original_initialize = ChampionAgentEpisode.initialize_pristine

                def crash_after_child(*args, **kwargs):
                    child = original_initialize(*args, **kwargs)
                    created["episode_id"] = child.environment.episode.episode_id
                    raise RuntimeError("injected crash after durable child")

                with patch(
                    "autosport.champion_agent_episode.load_champion_policy",
                    return_value=policy,
                ), patch.object(
                    ChampionAgentEpisode,
                    "initialize_pristine",
                    side_effect=crash_after_child,
                ):
                    with self.assertRaisesRegex(
                        RuntimeError, "injected crash after durable child"
                    ):
                        self._start(
                            handoff,
                            root,
                            environment,
                            snapshot,
                        )

                self.assertTrue((root / "child-agent-loop.json").exists())
                handoff.state_path.unlink()
                recreated = PaperCampaignEpisodeHandoff(runtime)

                with patch(
                    "autosport.champion_agent_episode.load_champion_policy",
                    return_value=policy,
                ):
                    with self.assertRaisesRegex(
                        PaperCampaignEpisodeHandoffError,
                        "intent conflicts with durable reservation",
                    ):
                        self._start(
                            recreated,
                            root,
                            environment,
                            snapshot,
                            child_name="other-child-agent-loop.json",
                            episode_key="campaign-episode-3",
                            loop_id="campaign-loop-3",
                            at=_legacy.T5,
                        )
                self.assertFalse((root / "other-child-agent-loop.json").exists())

                with patch(
                    "autosport.champion_agent_episode.load_champion_policy",
                    return_value=policy,
                ):
                    recovered = self._start(
                        recreated,
                        root,
                        environment,
                        snapshot,
                    )
                self.assertEqual(
                    recovered.episode.environment.episode.episode_id,
                    created["episode_id"],
                )

    def test_committed_parent_consumption_survives_handoff_file_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "workspace"
            root.mkdir()
            with patch.dict(
                os.environ,
                {"AUTOSPORT_MONOTONIC_AUTHORITY_ROOT": str(base / "authority")},
            ):
                environment, runtime = self._terminal_parent(root)
                snapshot = runtime.agent_loop.snapshot()
                policy = self._policy(environment)
                handoff = PaperCampaignEpisodeHandoff(runtime)
                with patch(
                    "autosport.champion_agent_episode.load_champion_policy",
                    return_value=policy,
                ):
                    first = self._start(
                        handoff,
                        root,
                        environment,
                        snapshot,
                    )
                self.assertTrue(first.receipt.handoff_id)

                handoff.state_path.unlink()
                recreated = PaperCampaignEpisodeHandoff(runtime)
                with patch(
                    "autosport.champion_agent_episode.load_champion_policy",
                    return_value=policy,
                ):
                    with self.assertRaisesRegex(
                        PaperCampaignEpisodeHandoffError,
                        "consumption state is missing, rolled back, or unproven",
                    ):
                        self._start(
                            recreated,
                            root,
                            environment,
                            snapshot,
                            child_name="other-child-agent-loop.json",
                            episode_key="campaign-episode-3",
                            loop_id="campaign-loop-3",
                            at=_legacy.T5,
                        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
