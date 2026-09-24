from __future__ import annotations

from pathlib import Path

import pytest

import autosport.execution_stop_authority as stop_module
from autosport.execution_stop_authority import (
    ExecutionAuthorityMode,
    ExecutionStopAuthority,
    ExecutionStopAuthorityError,
    ExecutionStopIntegrityError,
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


def test_process_environment_root_retarget_cannot_reauthorize_valid_old_armed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "workspace" / "execution-stop.jsonl"
    caller_root_a = tmp_path / "caller-root-a"
    caller_root_b = tmp_path / "caller-root-b"

    product_root = ExecutionStopAuthority._product_monotonic_authority_root()
    if stop_module.os.name == "nt":
        assert product_root.parts[-3:] == (
            "Autosport",
            "application-state",
            "monotonic-authority-v1",
        )
    else:
        assert product_root.parts[-3:] == (
            "state",
            "autosport",
            "monotonic-authority-v1",
        )

    monkeypatch.setenv(
        "AUTOSPORT_MONOTONIC_AUTHORITY_ROOT",
        str(caller_root_a),
    )
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "caller-local-app-data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "caller-xdg-state"))
    monkeypatch.setenv("HOME", str(tmp_path / "caller-home"))

    authority = _initialized(path)
    assert authority._monotonic_authority().authority_root == product_root
    armed = authority.arm(
        operator_id="owner",
        reason="supervised arm",
        confirmation_id="confirm-r2-root-retarget",
        expected_revision=1,
        command_id="arm-r2-root-retarget",
    )
    assert armed.mode is ExecutionAuthorityMode.ARMED

    valid_old_armed_journal = path.read_bytes()
    valid_old_armed_anchor = authority.anchor_path.read_bytes()

    stopped = authority.stop(
        operator_id="owner",
        reason="newer emergency stop",
        expected_revision=2,
        command_id="stop-r3-root-retarget",
    )
    assert stopped.mode is ExecutionAuthorityMode.STOPPED

    # Restore a mutually-consistent but superseded ARMED local pair, then
    # simulate a fresh process whose caller-controlled generic authority root
    # points somewhere empty.
    path.write_bytes(valid_old_armed_journal)
    authority.anchor_path.write_bytes(valid_old_armed_anchor)
    monkeypatch.setenv(
        "AUTOSPORT_MONOTONIC_AUTHORITY_ROOT",
        str(caller_root_b),
    )
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "caller-local-app-data-b"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "caller-xdg-state-b"))
    monkeypatch.setenv("HOME", str(tmp_path / "caller-home-b"))

    restarted = ExecutionStopAuthority(path)
    assert restarted._monotonic_authority().authority_root == product_root
    assert restarted.decision().allowed is False
    with pytest.raises(ExecutionStopAuthorityError):
        restarted.assert_execution_allowed()

    # The supported STOP path must not create either caller-selected authority
    # root while resolving the already-committed monotonic high-water mark.
    assert not caller_root_a.exists()
    assert not caller_root_b.exists()


def test_admission_lease_rejects_product_root_selector_class_rebind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _initialized(tmp_path / "execution-stop.jsonl")
    authority.arm(
        operator_id="owner",
        reason="supervised arm",
        confirmation_id="confirm-root-selector-guard",
        expected_revision=1,
        command_id="arm-root-selector-guard",
    )
    forged_calls: list[str] = []

    def forged_root() -> Path:
        forged_calls.append("called")
        return tmp_path / "forged-authority-root"

    monkeypatch.setattr(
        ExecutionStopAuthority,
        "_product_monotonic_authority_root",
        staticmethod(forged_root),
    )

    with pytest.raises(
        ExecutionStopAuthorityError,
        match="canonical execution admission helper graph changed",
    ):
        with authority.admission_lease():
            pytest.fail("rebound product root selector yielded an execution lease")

    assert forged_calls == []



def test_admission_lease_rejects_monotonic_recover_class_substitution_on_valid_old_armed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "execution-stop.jsonl"
    authority = _initialized(path)
    armed = authority.arm(
        operator_id="owner",
        reason="supervised arm",
        confirmation_id="confirm-r2-recover-substitution",
        expected_revision=1,
        command_id="arm-r2-recover-substitution",
    )
    assert armed.mode is ExecutionAuthorityMode.ARMED

    valid_old_armed_journal = path.read_bytes()
    valid_old_armed_anchor = authority.anchor_path.read_bytes()

    stopped = authority.stop(
        operator_id="owner",
        reason="newer emergency stop",
        expected_revision=2,
        command_id="stop-r3-recover-substitution",
    )
    assert stopped.mode is ExecutionAuthorityMode.STOPPED

    # Restore a mutually consistent local revision-2 ARMED pair while the
    # independent authority still carries the newer committed STOP high-water
    # mark. The genuine recover() must reject this rollback.
    path.write_bytes(valid_old_armed_journal)
    authority.anchor_path.write_bytes(valid_old_armed_anchor)
    restarted = ExecutionStopAuthority(path)
    forged_calls: list[str] = []

    def no_op_recover(
        _self,
        *,
        observed_state_sha256,
        tx_id=None,
        semantic_binding_sha256=None,
    ):
        forged_calls.append("called")
        return None

    monkeypatch.setattr(
        stop_module.MonotonicWorkspaceAuthority,
        "recover",
        no_op_recover,
    )

    with pytest.raises(
        ExecutionStopIntegrityError,
        match="canonical monotonic authority helper graph changed",
    ):
        with restarted.admission_lease():
            pytest.fail(
                "valid-old ARMED state survived monotonic recover substitution"
            )

    assert forged_calls == []
