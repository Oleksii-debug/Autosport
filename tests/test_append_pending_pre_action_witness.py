from types import SimpleNamespace

import pytest

from autosport.live_decision_loop import LiveDecisionProgressError, PersistentLiveDecisionLoop


def test_append_pending_without_execution_requires_durable_pre_action_book(tmp_path):
    loop = SimpleNamespace(
        _progress=SimpleNamespace(phase="append_pending"),
        paper_execution=None,
        pre_action_book_path=tmp_path / "live_decision_pre_action_book.json",
    )

    with pytest.raises(
        LiveDecisionProgressError,
        match="missing durable pre-action PaperBook witness",
    ):
        PersistentLiveDecisionLoop._recover_unfinished_progress(loop)
