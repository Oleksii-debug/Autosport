from types import SimpleNamespace

import pytest

import autosport._paper_execution_append_recovery  # noqa: F401
from autosport import live_decision_loop as live


def test_append_pending_without_pre_action_witness_fails_closed(tmp_path):
    loop = SimpleNamespace(
        _progress=SimpleNamespace(phase="append_pending"),
        paper_execution=None,
        pre_action_book_path=tmp_path / "live_decision_pre_action_book.json",
    )

    with pytest.raises(
        live.LiveDecisionProgressError,
        match="append-pending recovery requires exact pre-action PaperBook witness",
    ):
        live.PersistentLiveDecisionLoop._recover_unfinished_progress(loop)

def test_append_pending_with_paper_execution_requires_pre_action_witness(tmp_path):
    loop = SimpleNamespace(
        _progress=SimpleNamespace(phase="append_pending"),
        paper_execution=object(),
        pre_action_book_path=tmp_path / "live_decision_pre_action_book.json",
    )

    with pytest.raises(
        live.LiveDecisionProgressError,
        match="append-pending recovery requires exact pre-action PaperBook witness",
    ):
        live.PersistentLiveDecisionLoop._recover_unfinished_progress(loop)

