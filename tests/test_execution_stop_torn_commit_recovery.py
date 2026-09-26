from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from autosport.execution_stop_authority import (
    ExecutionAuthorityMode,
    ExecutionStopAuthority,
    ExecutionStopIntegrityError,
    ExecutionStopStateError,
)


def _authority(path: Path) -> ExecutionStopAuthority:
    return ExecutionStopAuthority(path)


def _init_stopped(path: Path) -> ExecutionStopAuthority:
    store = _authority(path)
    store.initialize_stopped(
        operator_id="owner",
        reason="initial safe state",
        command_id="init-stop",
    )
    return store


def _init_armed(path: Path) -> ExecutionStopAuthority:
    store = _init_stopped(path)
    store.arm(
        operator_id="owner",
        reason="supervised approval",
        confirmation_id="confirm-committed",
        expected_revision=1,
        command_id="arm-committed",
    )
    return store


def _leave_torn_arm(
    store: ExecutionStopAuthority,
    monkeypatch: pytest.MonkeyPatch,
    *,
    expected_revision: int,
    command_id: str = "arm-torn",
    confirmation_id: str = "confirm-torn",
) -> None:
    def fail_anchor(*, revision: int, record_sha256: str) -> None:
        assert revision == expected_revision + 1
        assert len(record_sha256) == 64
        raise ExecutionStopIntegrityError("injected pre-anchor failure")

    monkeypatch.setattr(store, "_write_anchor_unlocked", fail_anchor)
    with pytest.raises(ExecutionStopIntegrityError, match="injected"):
        store.arm(
            operator_id="owner",
            reason="torn arm",
            confirmation_id=confirmation_id,
            expected_revision=expected_revision,
            command_id=command_id,
        )


def _leave_torn_stop(
    store: ExecutionStopAuthority,
    monkeypatch: pytest.MonkeyPatch,
    *,
    expected_revision: int,
    command_id: str = "stop-torn",
) -> None:
    def fail_anchor(*, revision: int, record_sha256: str) -> None:
        assert revision == expected_revision + 1
        assert len(record_sha256) == 64
        raise ExecutionStopIntegrityError("injected pre-anchor failure")

    monkeypatch.setattr(store, "_write_anchor_unlocked", fail_anchor)
    with pytest.raises(ExecutionStopIntegrityError, match="injected"):
        store.stop(
            operator_id="owner",
            reason="torn stop",
            expected_revision=expected_revision,
            command_id=command_id,
        )


def test_recovery_discards_unanchored_arm_without_promoting_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "execution-stop.jsonl"
    store = _init_stopped(path)
    _leave_torn_arm(store, monkeypatch, expected_revision=1)

    restarted = _authority(path)
    assert restarted.decision().allowed is False

    recovered = restarted.recover_torn_transition()

    assert recovered.revision == 1
    assert recovered.mode is ExecutionAuthorityMode.STOPPED
    assert restarted.current() == recovered
    assert restarted.decision().allowed is False
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1


def test_recovery_commits_complete_unanchored_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "execution-stop.jsonl"
    store = _init_armed(path)
    _leave_torn_stop(store, monkeypatch, expected_revision=2)

    restarted = _authority(path)
    assert restarted.decision().allowed is False

    recovered = restarted.recover_torn_transition()

    assert recovered.revision == 3
    assert recovered.mode is ExecutionAuthorityMode.STOPPED
    assert recovered.command_id == "stop-torn"
    assert restarted.current() == recovered
    assert restarted.decision().allowed is False


def test_consistent_authority_recovery_is_idempotent_noop(tmp_path: Path) -> None:
    path = tmp_path / "execution-stop.jsonl"
    store = _init_armed(path)
    journal_before = path.read_bytes()
    anchor_before = store.anchor_path.read_bytes()

    first = store.recover_torn_transition()
    second = _authority(path).recover_torn_transition()

    assert first == second == store.current()
    assert first.mode is ExecutionAuthorityMode.ARMED
    assert path.read_bytes() == journal_before
    assert store.anchor_path.read_bytes() == anchor_before


def test_recovery_rejects_more_than_one_unanchored_record(tmp_path: Path) -> None:
    path = tmp_path / "execution-stop.jsonl"
    store = _init_armed(path)
    anchor_revision_two = store.anchor_path.read_bytes()

    store.stop(
        operator_id="owner",
        reason="committed stop",
        expected_revision=2,
        command_id="stop-committed",
    )
    store.arm(
        operator_id="owner",
        reason="second approval",
        confirmation_id="confirm-second",
        expected_revision=3,
        command_id="arm-second",
    )
    store.anchor_path.write_bytes(anchor_revision_two)

    with pytest.raises(
        ExecutionStopIntegrityError,
        match="exactly one unanchored record",
    ):
        _authority(path).recover_torn_transition()


def test_recovery_rejects_anchor_from_different_valid_journal(tmp_path: Path) -> None:
    first_path = tmp_path / "first.jsonl"
    first = _init_stopped(first_path)

    other_path = tmp_path / "other.jsonl"
    other = _authority(other_path)
    other.initialize_stopped(
        operator_id="different-owner",
        reason="different safe state",
        command_id="different-init",
    )
    first.anchor_path.write_bytes(other.anchor_path.read_bytes())

    with pytest.raises(
        ExecutionStopIntegrityError,
        match="does not match its journal prefix",
    ):
        _authority(first_path).recover_torn_transition()


def test_recovery_establishes_first_anchor_for_complete_initial_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "execution-stop.jsonl"
    store = _authority(path)

    def fail_first_anchor(*, revision: int, record_sha256: str) -> None:
        assert revision == 1
        assert len(record_sha256) == 64
        raise ExecutionStopIntegrityError("injected first-anchor failure")

    monkeypatch.setattr(store, "_write_anchor_unlocked", fail_first_anchor)
    with pytest.raises(ExecutionStopIntegrityError, match="first-anchor"):
        store.initialize_stopped(
            operator_id="owner",
            reason="initial safe state",
            command_id="init-torn",
        )
    monkeypatch.undo()

    restarted = _authority(path)
    assert path.exists()
    assert not restarted.anchor_path.exists()
    assert restarted.decision().allowed is False

    recovered = restarted.recover_torn_transition()
    assert recovered.revision == 1
    assert recovered.mode is ExecutionAuthorityMode.STOPPED
    assert recovered.command_id == "init-torn"
    assert restarted.anchor_path.exists()
    assert restarted.current() == recovered
    assert restarted.recover_torn_transition() == recovered


def test_recovery_rejects_missing_journal_with_existing_anchor(tmp_path: Path) -> None:
    path = tmp_path / "execution-stop.jsonl"
    store = _init_stopped(path)
    path.unlink()

    with pytest.raises(
        ExecutionStopIntegrityError,
        match="journal/anchor pair is incomplete",
    ):
        _authority(path).recover_torn_transition()


def test_recovery_rejects_multiple_records_without_anchor(tmp_path: Path) -> None:
    path = tmp_path / "execution-stop.jsonl"
    store = _init_armed(path)
    store.anchor_path.unlink()

    with pytest.raises(
        ExecutionStopIntegrityError,
        match="cannot establish an ambiguous first anchor",
    ):
        _authority(path).recover_torn_transition()


def test_recovery_never_establishes_first_anchor_for_armed_record(tmp_path: Path) -> None:
    path = tmp_path / "execution-stop.jsonl"
    store = _init_stopped(path)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["mode"] = ExecutionAuthorityMode.ARMED.value
    record["confirmation_id"] = "forged-initial-arm"
    body = {key: value for key, value in record.items() if key != "record_sha256"}
    canonical = json.dumps(
        body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    record["record_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    path.write_text(
        json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    store.anchor_path.unlink()

    restarted = _authority(path)
    assert restarted.decision().allowed is False
    with pytest.raises(
        ExecutionStopIntegrityError,
        match="cannot establish an ambiguous first anchor",
    ):
        restarted.recover_torn_transition()
    assert not restarted.anchor_path.exists()


def test_arm_rollback_recovery_is_safe_if_interrupted_after_journal_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "execution-stop.jsonl"
    store = _init_stopped(path)
    _leave_torn_arm(store, monkeypatch, expected_revision=1)
    monkeypatch.undo()

    recovering = _authority(path)
    durable_rewrite = recovering._rewrite_journal_unlocked

    def rewrite_then_interrupt(records: list[dict[str, object]]) -> None:
        durable_rewrite(records)
        raise ExecutionStopIntegrityError("injected recovery interruption")

    monkeypatch.setattr(
        recovering,
        "_rewrite_journal_unlocked",
        rewrite_then_interrupt,
    )
    with pytest.raises(ExecutionStopIntegrityError, match="recovery interruption"):
        recovering.recover_torn_transition()

    restarted = _authority(path)
    recovered = restarted.recover_torn_transition()
    assert recovered.revision == 1
    assert recovered.mode is ExecutionAuthorityMode.STOPPED
    assert restarted.decision().allowed is False


def test_stop_recovery_is_safe_if_interrupted_after_anchor_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "execution-stop.jsonl"
    store = _init_armed(path)
    _leave_torn_stop(store, monkeypatch, expected_revision=2)
    monkeypatch.undo()

    recovering = _authority(path)
    durable_anchor = recovering._write_anchor_unlocked

    def anchor_then_interrupt(*, revision: int, record_sha256: str) -> None:
        durable_anchor(revision=revision, record_sha256=record_sha256)
        raise ExecutionStopIntegrityError("injected recovery interruption")

    monkeypatch.setattr(
        recovering,
        "_write_anchor_unlocked",
        anchor_then_interrupt,
    )
    with pytest.raises(ExecutionStopIntegrityError, match="recovery interruption"):
        recovering.recover_torn_transition()

    restarted = _authority(path)
    recovered = restarted.recover_torn_transition()
    assert recovered.revision == 3
    assert recovered.mode is ExecutionAuthorityMode.STOPPED
    assert restarted.decision().allowed is False


def test_committed_replay_guards_survive_torn_arm_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "execution-stop.jsonl"
    store = _init_armed(path)
    store.stop(
        operator_id="owner",
        reason="committed stop",
        expected_revision=2,
        command_id="stop-committed",
    )
    _leave_torn_arm(
        store,
        monkeypatch,
        expected_revision=3,
        command_id="arm-uncommitted",
        confirmation_id="confirm-uncommitted",
    )
    monkeypatch.undo()

    recovered = _authority(path)
    state = recovered.recover_torn_transition()
    assert state.revision == 3
    assert state.mode is ExecutionAuthorityMode.STOPPED

    with pytest.raises(ExecutionStopStateError, match="confirmation replay"):
        recovered.arm(
            operator_id="owner",
            reason="must not reuse committed approval",
            confirmation_id="confirm-committed",
            expected_revision=3,
            command_id="new-command",
        )

    with pytest.raises(ExecutionStopStateError, match="command replay"):
        recovered.stop(
            operator_id="owner",
            reason="must not reuse committed command",
            expected_revision=3,
            command_id="stop-committed",
        )
