from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from autosport.decision_ledger import (
    DecisionLedgerIntegrityError,
    DecisionRecord,
    JsonlDecisionLedger,
)


class _TrapStr(str):
    def strip(self, *args: object, **kwargs: object) -> str:
        raise AssertionError("str subclass strip must not execute")

    def encode(self, *args: object, **kwargs: object) -> bytes:
        raise AssertionError("str subclass encode must not execute")

    def __hash__(self) -> int:
        raise AssertionError("str subclass hash must not execute")


def _record(**overrides: object) -> DecisionRecord:
    values: dict[str, object] = {
        "replay_run_id": "run-1",
        "agent": "agent-1",
        "observed_ts": "2026-10-07T00:00:00+00:00",
        "action": "OBSERVE",
        "payload": {"x": 1},
        "context_hash": "context-1",
        "decision_id": "decision-1",
        "recorded_at": "2026-10-07T00:00:01+00:00",
        "decision_kind": "GENERAL",
    }
    values.update(overrides)
    return DecisionRecord(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "field_name",
    (
        "replay_run_id",
        "agent",
        "observed_ts",
        "action",
        "context_hash",
        "recorded_at",
        "decision_kind",
    ),
)
def test_constructor_rejects_top_level_identity_str_subclass_before_virtual_dispatch(
    field_name: str,
) -> None:
    with pytest.raises(ValueError, match="canonical string"):
        _record(**{field_name: _TrapStr("GENERAL" if field_name == "decision_kind" else "x")})


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("replay_run_id", " run-1"),
        ("agent", "agent-1 "),
        ("action", "OBS\nERVE"),
        ("context_hash", "context-1\x00alias"),
        ("recorded_at", "2026-10-07T00:00:01+00:00\ud800"),
    ),
)
def test_constructor_rejects_noncanonical_top_level_identity_text(
    field_name: str,
    value: str,
) -> None:
    with pytest.raises(ValueError):
        _record(**{field_name: value})


def test_append_revalidates_tampered_top_level_identity_before_virtual_dispatch(
    tmp_path: Path,
) -> None:
    path = tmp_path / "decisions.jsonl"
    record = _record()
    object.__setattr__(record, "replay_run_id", _TrapStr("run-1"))

    with pytest.raises(
        DecisionLedgerIntegrityError,
        match="replay_run_id.*invalid",
    ):
        JsonlDecisionLedger(path).append(record)

    assert not path.exists()


def test_restart_rejects_hash_consistent_noncanonical_top_level_identity(
    tmp_path: Path,
) -> None:
    path = tmp_path / "decisions.jsonl"
    record = _record().to_dict()
    record["agent"] = " agent-1"
    canonical = JsonlDecisionLedger._canonical_record(record)
    envelope = {
        "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "record": record,
    }
    path.write_text(
        json.dumps(envelope, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        DecisionLedgerIntegrityError,
        match="agent.*invalid",
    ):
        JsonlDecisionLedger(path).verified_records()
