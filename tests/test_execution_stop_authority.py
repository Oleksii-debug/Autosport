from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from autosport.execution_stop_authority import (
    ExecutionAuthorityMode,
    ExecutionStopAuthority,
    ExecutionStopIntegrityError,
    ExecutionStopStateError,
    ExecutionStoppedError,
)


NOW = datetime(2026, 9, 21, 7, 45, tzinfo=timezone.utc)


def authority(tmp_path):
    return ExecutionStopAuthority(
        tmp_path / "execution-stop.jsonl",
        clock=lambda: NOW,
    )


def test_missing_authority_fails_closed(tmp_path):
    store = authority(tmp_path)

    decision = store.decision()

    assert decision.allowed is False
    assert decision.mode is ExecutionAuthorityMode.STOPPED
    assert decision.revision is None
    with pytest.raises(ExecutionStopStateError):
        store.assert_execution_allowed()


def test_initialize_is_stopped_and_restart_stable(tmp_path):
    store = authority(tmp_path)
    initialized = store.initialize_stopped(
        operator_id="owner",
        reason="initial safe state",
        command_id="cmd-init",
    )

    restarted = authority(tmp_path)

    assert initialized.revision == 1
    assert initialized.mode is ExecutionAuthorityMode.STOPPED
    assert restarted.current() == initialized
    assert restarted.decision().allowed is False


def test_explicit_arm_allows_execution_and_survives_restart(tmp_path):
    store = authority(tmp_path)
    store.initialize_stopped(
        operator_id="owner",
        reason="initial safe state",
        command_id="cmd-init",
    )

    armed = store.arm(
        operator_id="owner",
        reason="supervised execution approved",
        confirmation_id="owner-confirmation-001",
        expected_revision=1,
        command_id="cmd-arm",
    )

    restarted = authority(tmp_path)
    assert armed.revision == 2
    assert restarted.decision().allowed is True
    assert restarted.assert_execution_allowed() == armed


def test_stop_revokes_execution_and_never_auto_rearms(tmp_path):
    store = authority(tmp_path)
    store.initialize_stopped(
        operator_id="owner", reason="safe", command_id="cmd-init"
    )
    store.arm(
        operator_id="owner",
        reason="approved",
        confirmation_id="confirm-1",
        expected_revision=1,
        command_id="cmd-arm",
    )

    stopped = store.stop(
        operator_id="owner",
        reason="owner emergency stop",
        expected_revision=2,
        command_id="cmd-stop",
    )

    restarted = authority(tmp_path)
    assert stopped.mode is ExecutionAuthorityMode.STOPPED
    assert stopped.revision == 3
    assert restarted.decision().allowed is False
    with pytest.raises(ExecutionStoppedError):
        restarted.assert_execution_allowed()


def test_stop_can_establish_missing_authority_in_safe_state(tmp_path):
    store = authority(tmp_path)

    state = store.stop(
        operator_id="owner",
        reason="fail safe before setup",
        command_id="cmd-stop-first",
    )

    assert state.revision == 1
    assert state.mode is ExecutionAuthorityMode.STOPPED
    assert store.decision().allowed is False


def test_arm_requires_initialized_state(tmp_path):
    store = authority(tmp_path)

    with pytest.raises(ExecutionStopStateError, match="initialize STOPPED"):
        store.arm(
            operator_id="owner",
            reason="unsafe shortcut",
            confirmation_id="confirm-1",
            expected_revision=0,
        )


def test_stale_revision_is_rejected(tmp_path):
    store = authority(tmp_path)
    store.initialize_stopped(
        operator_id="owner", reason="safe", command_id="cmd-init"
    )
    store.arm(
        operator_id="owner",
        reason="approved",
        confirmation_id="confirm-1",
        expected_revision=1,
        command_id="cmd-arm",
    )

    with pytest.raises(ExecutionStopStateError, match="stale"):
        store.stop(
            operator_id="owner",
            reason="stale writer",
            expected_revision=1,
            command_id="cmd-stale",
        )

    assert store.current().revision == 2
    assert store.current().mode is ExecutionAuthorityMode.ARMED


def test_command_replay_is_rejected(tmp_path):
    store = authority(tmp_path)
    store.initialize_stopped(
        operator_id="owner", reason="safe", command_id="same-command"
    )

    with pytest.raises(ExecutionStopStateError, match="command replay"):
        store.stop(
            operator_id="owner",
            reason="replay",
            expected_revision=1,
            command_id="same-command",
        )


def test_arm_confirmation_replay_is_rejected_even_after_stop(tmp_path):
    store = authority(tmp_path)
    store.initialize_stopped(
        operator_id="owner", reason="safe", command_id="init"
    )
    store.arm(
        operator_id="owner",
        reason="approved",
        confirmation_id="confirm-once",
        expected_revision=1,
        command_id="arm-1",
    )
    store.stop(
        operator_id="owner",
        reason="stop",
        expected_revision=2,
        command_id="stop-1",
    )

    with pytest.raises(ExecutionStopStateError, match="confirmation replay"):
        store.arm(
            operator_id="owner",
            reason="attempt reuse",
            confirmation_id="confirm-once",
            expected_revision=3,
            command_id="arm-2",
        )


def test_corrupt_journal_fails_closed(tmp_path):
    store = authority(tmp_path)
    store.initialize_stopped(
        operator_id="owner", reason="safe", command_id="init"
    )
    store.arm(
        operator_id="owner",
        reason="approved",
        confirmation_id="confirm-1",
        expected_revision=1,
        command_id="arm",
    )
    store.path.write_text("{broken\n", encoding="utf-8")

    decision = store.decision()

    assert decision.allowed is False
    assert decision.mode is ExecutionAuthorityMode.STOPPED
    assert decision.revision is None
    with pytest.raises(ExecutionStopIntegrityError):
        store.assert_execution_allowed()


def test_truncated_journal_is_rejected_against_anchor(tmp_path):
    store = authority(tmp_path)
    store.initialize_stopped(
        operator_id="owner", reason="safe", command_id="init"
    )
    store.arm(
        operator_id="owner",
        reason="approved",
        confirmation_id="confirm-1",
        expected_revision=1,
        command_id="arm",
    )
    lines = store.path.read_text(encoding="utf-8").splitlines()
    store.path.write_text(lines[0] + "\n", encoding="utf-8")

    with pytest.raises(ExecutionStopIntegrityError, match="older or newer"):
        store.current()
    assert store.decision().allowed is False


def test_tampered_anchor_fails_closed(tmp_path):
    store = authority(tmp_path)
    store.initialize_stopped(
        operator_id="owner", reason="safe", command_id="init"
    )
    store.arm(
        operator_id="owner",
        reason="approved",
        confirmation_id="confirm-1",
        expected_revision=1,
        command_id="arm",
    )
    anchor = json.loads(store.anchor_path.read_text(encoding="utf-8"))
    anchor["revision"] = 1
    store.anchor_path.write_text(json.dumps(anchor), encoding="utf-8")

    assert store.decision().allowed is False
    with pytest.raises(ExecutionStopIntegrityError):
        store.current()


def test_duplicate_json_key_fails_closed(tmp_path):
    store = authority(tmp_path)
    store.initialize_stopped(
        operator_id="owner", reason="safe", command_id="init"
    )
    raw = store.path.read_text(encoding="utf-8").rstrip()
    duplicate = raw[:-1] + ',"mode":"ARMED"}\n'
    store.path.write_text(duplicate, encoding="utf-8")

    assert store.decision().allowed is False


def test_initialize_cannot_overwrite_existing_authority(tmp_path):
    store = authority(tmp_path)
    store.initialize_stopped(
        operator_id="owner", reason="safe", command_id="init"
    )

    with pytest.raises(ExecutionStopStateError, match="already initialized"):
        store.initialize_stopped(
            operator_id="owner",
            reason="replace",
            command_id="replace",
        )
