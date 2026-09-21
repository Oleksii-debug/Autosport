from __future__ import annotations

from pathlib import Path

import pytest

from autosport.execution_stop_authority import (
    ExecutionAuthorityMode,
    ExecutionStopAuthority,
    ExecutionStopAuthorityError,
)


def _initialized(path: Path) -> ExecutionStopAuthority:
    authority = ExecutionStopAuthority(path)
    authority.initialize_stopped(
        operator_id="owner",
        reason="initial safe state",
        command_id="stop-r1",
    )
    return authority


def test_valid_old_armed_pair_cannot_resurrect_execution_after_newer_stop(
    tmp_path: Path,
) -> None:
    path = tmp_path / "execution-stop.jsonl"
    authority = _initialized(path)

    armed = authority.arm(
        operator_id="owner",
        reason="supervised arm",
        confirmation_id="confirm-r2",
        expected_revision=1,
        command_id="arm-r2",
    )
    assert armed.revision == 2
    assert armed.mode is ExecutionAuthorityMode.ARMED
    assert authority.decision().allowed is True

    old_armed_journal = path.read_bytes()
    old_armed_anchor = authority.anchor_path.read_bytes()

    stopped = authority.stop(
        operator_id="owner",
        reason="newer emergency stop",
        expected_revision=2,
        command_id="stop-r3",
    )
    assert stopped.revision == 3
    assert stopped.mode is ExecutionAuthorityMode.STOPPED
    assert authority.decision().allowed is False

    # Simulate rollback of the entire workspace-local STOP authority pair.
    # Both files remain individually valid and mutually consistent at revision 2.
    path.write_bytes(old_armed_journal)
    authority.anchor_path.write_bytes(old_armed_anchor)

    restarted = ExecutionStopAuthority(path)

    # Product law: an independently committed revision 3 STOP is a monotonic
    # high-water mark. Restoring valid-but-older local bytes must fail closed;
    # it must never resurrect the superseded ARMED permission.
    assert restarted.decision().allowed is False
    with pytest.raises(ExecutionStopAuthorityError):
        restarted.assert_execution_allowed()


def test_deleting_local_pair_cannot_rebootstrap_after_committed_history(
    tmp_path: Path,
) -> None:
    path = tmp_path / "execution-stop.jsonl"
    authority = _initialized(path)
    authority.arm(
        operator_id="owner",
        reason="supervised arm",
        confirmation_id="confirm-r2",
        expected_revision=1,
        command_id="arm-r2",
    )
    authority.stop(
        operator_id="owner",
        reason="committed stop",
        expected_revision=2,
        command_id="stop-r3",
    )
    assert authority.decision().allowed is False

    path.unlink()
    authority.anchor_path.unlink()

    restarted = ExecutionStopAuthority(path)

    # Once independent monotonic history exists, deleting rollbackable local
    # bytes must not create a fresh authority namespace with revision 1.
    with pytest.raises(ExecutionStopAuthorityError):
        restarted.initialize_stopped(
            operator_id="owner",
            reason="must not rebootstrap lost history",
            command_id="replacement-stop-r1",
        )
