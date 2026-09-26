from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import autosport.paper_campaign_episode_handoff as handoff_module
from autosport.paper_campaign_episode_handoff import (
    PaperCampaignEpisodeHandoff,
    PaperCampaignEpisodeHandoffError,
)


_BASE_PATH = Path(__file__).with_name("test_paper_campaign_episode_handoff.py")
_SPEC = importlib.util.spec_from_file_location(
    "_paper_campaign_episode_handoff_existing_tests_base",
    _BASE_PATH,
)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover - import guard
    raise RuntimeError("cannot load episode handoff regression base")
_base = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_base)


class PaperCampaignEpisodeHandoffOpenExistingTests(unittest.TestCase):
    @staticmethod
    def _committed_handoff(root: Path):
        environment, runtime, _finalization = (
            _base.PaperCampaignEpisodeHandoffTests._terminal_parent(root)
        )
        parent_snapshot = runtime.agent_loop.snapshot()
        handoff = PaperCampaignEpisodeHandoff(runtime)
        policy = _base.PaperCampaignEpisodeHandoffTests._child_policy(environment)
        with patch(
            "autosport.champion_agent_episode.load_champion_policy",
            return_value=policy,
        ):
            result = _base.PaperCampaignEpisodeHandoffTests._call(
                handoff,
                root,
                environment,
                parent_snapshot,
            )
        return runtime, handoff, result

    def test_open_existing_preserves_committed_locator_and_state_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime, handoff, result = self._committed_handoff(root)
            before = handoff.state_path.read_bytes()

            reopened = PaperCampaignEpisodeHandoff.open_existing(runtime)
            records = reopened.committed_children()

            self.assertEqual(handoff.state_path.read_bytes(), before)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].handoff_id, result.receipt.handoff_id)
            self.assertEqual(
                records[0].child_episode_id,
                result.receipt.child_episode_id,
            )

    def test_missing_handoff_state_after_commit_fails_without_recreation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime, handoff, _result = self._committed_handoff(root)
            state_path = handoff.state_path
            state_path.unlink()
            self.assertFalse(state_path.exists())

            with self.assertRaisesRegex(
                PaperCampaignEpisodeHandoffError,
                "existing handoff state is missing for restart readback",
            ):
                PaperCampaignEpisodeHandoff.open_existing(runtime)

            self.assertFalse(state_path.exists())

    def test_local_prepared_downgrade_cannot_hide_committed_child(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime, handoff, result = self._committed_handoff(root)
            state = json.loads(handoff.state_path.read_text(encoding="utf-8"))
            self.assertEqual(len(state["handoffs"]), 1)
            record = next(iter(state["handoffs"].values()))
            self.assertEqual(record["status"], "COMMITTED")
            self.assertEqual(record["handoff_id"], result.receipt.handoff_id)

            # Roll back only the mutable local projection to an otherwise valid
            # PREPARED record. The independent consumption authority remains
            # COMMIT and therefore has stronger restart truth.
            record["status"] = "PREPARED"
            record["child_policy_id"] = None
            record["child_episode_id"] = None
            record["child_initial_checkpoint_id"] = None
            record["handoff_id"] = None
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

            reopened = PaperCampaignEpisodeHandoff.open_existing(runtime)
            with self.assertRaisesRegex(
                PaperCampaignEpisodeHandoffError,
                "conflicts with independent committed consumption authority",
            ):
                reopened.committed_children()

    def test_valid_virgin_snapshot_cannot_omit_independently_committed_child(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment, runtime, _finalization = (
                _base.PaperCampaignEpisodeHandoffTests._terminal_parent(root)
            )
            parent_snapshot = runtime.agent_loop.snapshot()
            handoff = PaperCampaignEpisodeHandoff(runtime)
            virgin = handoff.state_path.read_bytes()
            policy = _base.PaperCampaignEpisodeHandoffTests._child_policy(environment)

            with patch(
                "autosport.champion_agent_episode.load_champion_policy",
                return_value=policy,
            ):
                result = _base.PaperCampaignEpisodeHandoffTests._call(
                    handoff,
                    root,
                    environment,
                    parent_snapshot,
                )

            committed = handoff.state_path.read_bytes()
            self.assertNotEqual(committed, virgin)
            self.assertEqual(
                json.loads(committed)["handoffs"][
                    result.receipt.parent_checkpoint_id
                ]["status"],
                "COMMITTED",
            )

            # Restore the exact byte-valid state that existed before the handoff.
            # Its local digest is honest, but it omits the child whose independent
            # consumption authority is already durably COMMIT.
            handoff.state_path.write_bytes(virgin)
            reopened = PaperCampaignEpisodeHandoff.open_existing(runtime)
            with self.assertRaisesRegex(
                PaperCampaignEpisodeHandoffError,
                "local handoff state omits independently committed child",
            ):
                reopened.committed_children()

            self.assertEqual(handoff.state_path.read_bytes(), virgin)

    def test_wrong_valid_state_path_after_commit_is_rejected_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime, handoff, _result = self._committed_handoff(root)
            canonical_before = handoff.state_path.read_bytes()
            wrong_path = root / "wrong-episode-handoff.json"
            wrong_writer = PaperCampaignEpisodeHandoff(runtime, state_path=wrong_path)
            wrong_before = wrong_writer.state_path.read_bytes()

            with self.assertRaisesRegex(
                PaperCampaignEpisodeHandoffError,
                "must match canonical campaign handoff path",
            ):
                PaperCampaignEpisodeHandoff.open_existing(
                    runtime,
                    state_path=wrong_path,
                )

            self.assertEqual(handoff.state_path.read_bytes(), canonical_before)
            self.assertEqual(wrong_path.read_bytes(), wrong_before)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
