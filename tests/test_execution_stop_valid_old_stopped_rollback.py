from __future__ import annotations

from pathlib import Path

import pytest

from autosport.execution_stop_authority import (
    ExecutionStopAuthority,
    ExecutionStopAuthorityError,
)


def test_valid_old_stopped_pair_cannot_erase_committed_replay_history(
    tmp_path: Path,
) -> None:
    path = tmp_path / "execution-stop.jsonl"
    authority = ExecutionStopAuthority(path)
    authority.initialize_stopped(
        operator_id="owner",
        reason="initial safe state",
        command_id="stop-r1",
    )

    old_stopped_journal = path.read_bytes()
    old_stopped_anchor = authority.anchor_path.read_bytes()

    authority.arm(
        operator_id="owner",
        reason="supervised approval",
        confirmation_id="confirm-r2",
        expected_revision=1,
        command_id="arm-r2",
    )
    authority.stop(
        operator_id="owner",
        reason="later committed stop",
        expected_revision=2,
        command_id="stop-r3",
    )
    assert authority.decision().allowed is False

    # Restore an internally valid older STOPPED pair. Immediate permission is still
    # denied, but accepting this rollback would erase later command/confirmation
    # history and could make committed replay identifiers appear unused.
    path.write_bytes(old_stopped_journal)
    authority.anchor_path.write_bytes(old_stopped_anchor)

    restarted = ExecutionStopAuthority(path)
    assert restarted.decision().allowed is False
    with pytest.raises(ExecutionStopAuthorityError):
        restarted.current()

    with pytest.raises(ExecutionStopAuthorityError):
        restarted.arm(
            operator_id="owner",
            reason="must not reuse hidden committed confirmation",
            confirmation_id="confirm-r2",
            expected_revision=1,
            command_id="arm-after-rollback",
        )

    with pytest.raises(ExecutionStopAuthorityError):
        restarted.stop(
            operator_id="owner",
            reason="must not reuse hidden committed command",
            expected_revision=1,
            command_id="stop-r3",
        )

    # Rejected replay attempts must not mutate the restored local bytes.
    assert path.read_bytes() == old_stopped_journal
    assert restarted.anchor_path.read_bytes() == old_stopped_anchor
