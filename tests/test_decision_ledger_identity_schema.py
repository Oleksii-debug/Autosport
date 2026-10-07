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
        "decision_id",
        "recorded_at",
        "decision_kind",
    ),
)
def test_constructor_rejects_identity_str_subclass_before_virtual_dispatch(
    field_name: str,
) -> None:
    with pytest.raises(ValueError, match="canonical string"):
        _record(**{field_name: _TrapStr("GENERAL" if field_name == "decision_kind" else "x")})


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("decision_id", " decision-1"),
        ("decision_id", "decision-1 "),
        ("replay_run_id", "run-1\x00alias"),
        ("context_hash", "context-\ud800"),
    ),
)
def test_constructor_rejects_noncanonical_identity_text(
    field_name: str,
    value: str,
) -> None:
    with pytest.raises(ValueError):
        _record(**{field_name: value})


def test_append_revalidates_tampered_identity_before_virtual_dispatch(
    tmp_path: Path,
) -> None:
    path = tmp_path / "decisions.jsonl"
    ledger = JsonlDecisionLedger(path)
    record = _record()
    object.__setattr__(record, "decision_id", _TrapStr("decision-1"))

    with pytest.raises(DecisionLedgerIntegrityError, match="decision_id.*invalid"):
        ledger.append(record)

    assert not path.exists()


def test_append_rejects_tampered_whitespace_alias_without_writing(
    tmp_path: Path,
) -> None:
    path = tmp_path / "decisions.jsonl"
    ledger = JsonlDecisionLedger(path)
    record = _record()
    object.__setattr__(record, "decision_id", " decision-1")

    with pytest.raises(DecisionLedgerIntegrityError, match="decision_id.*invalid"):
        ledger.append(record)

    assert not path.exists()


def test_restart_rejects_hash_consistent_noncanonical_decision_identity(
    tmp_path: Path,
) -> None:
    path = tmp_path / "decisions.jsonl"
    record = _record().to_dict()
    record["decision_id"] = " decision-1"
    canonical = JsonlDecisionLedger._canonical_record(record)
    envelope = {
        "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "record": record,
    }
    path.write_text(
        json.dumps(envelope, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(DecisionLedgerIntegrityError, match="decision_id.*invalid"):
        JsonlDecisionLedger(path).verified_records()


def test_valid_record_hash_contract_remains_stable_across_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "decisions.jsonl"
    record = _record()
    ledger = JsonlDecisionLedger(path)

    digest = ledger.append(record)
    restored = JsonlDecisionLedger(path).verified_records()

    assert restored == (record,)
    assert json.loads(path.read_text(encoding="utf-8"))["sha256"] == digest
