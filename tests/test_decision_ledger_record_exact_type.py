from pathlib import Path

import pytest

from autosport.decision_ledger import DecisionRecord, JsonlDecisionLedger


class _DecisionRecordAlias(DecisionRecord):
    pass


def test_decision_ledger_rejects_record_subclass(tmp_path: Path) -> None:
    record = _DecisionRecordAlias(
        replay_run_id="run-1",
        agent="agent-1",
        observed_ts="2026-10-07T00:00:00+00:00",
        action="OBSERVE",
        payload={"x": 1},
        context_hash="context-1",
        decision_id="decision-1",
        recorded_at="2026-10-07T00:00:01+00:00",
        decision_kind="GENERAL",
    )
    path = tmp_path / "decisions.jsonl"

    with pytest.raises(TypeError, match="exact DecisionRecord"):
        JsonlDecisionLedger(path).append(record)

    assert not path.exists()
