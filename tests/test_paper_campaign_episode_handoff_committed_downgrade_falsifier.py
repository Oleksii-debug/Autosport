"""Dependent falsifier for PR #843 committed-child restart downgrade."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest

import autosport.paper_campaign_episode_handoff as handoff_module
from autosport.paper_campaign_episode_handoff import (
    PaperCampaignEpisodeHandoff,
    PaperCampaignEpisodeHandoffError,
)


_BASE_PATH = Path(__file__).with_name("test_paper_campaign_episode_handoff.py")
_SPEC = importlib.util.spec_from_file_location(
    "_paper_campaign_episode_handoff_downgrade_base",
    _BASE_PATH,
)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover - import guard
    raise RuntimeError("cannot load episode handoff regression base")
_base = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_base)


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


def test_local_prepared_downgrade_cannot_hide_independently_committed_child(tmp_path: Path) -> None:
    runtime, handoff, result = _committed_handoff(tmp_path)
    state = json.loads(handoff.state_path.read_text(encoding="utf-8"))
    assert len(state["handoffs"]) == 1
    record = next(iter(state["handoffs"].values()))
    assert record["status"] == "COMMITTED"
    committed_handoff_id = record["handoff_id"]
    assert committed_handoff_id == result.receipt.handoff_id

    # Downgrade only mutable local state to the exact schema-valid PREPARED form.
    # The independent intent and consumption authorities are deliberately left
    # untouched, so they still prove that this parent checkpoint crossed COMMIT.
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

    # Restart truth must reconcile local status with the stronger independent
    # consumption COMMIT. Silently returning an empty inventory would forget a
    # durable child and permit the product to resume from the wrong episode.
    with pytest.raises(PaperCampaignEpisodeHandoffError):
        reopened.committed_children()
