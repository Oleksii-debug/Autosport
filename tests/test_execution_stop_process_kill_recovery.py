from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from autosport.execution_stop_authority import (
    ExecutionAuthorityMode,
    ExecutionStopAuthority,
    ExecutionStopIntegrityError,
)


def _child_env(authority_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    src_root = Path(__file__).resolve().parents[1] / "src"
    inherited = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(src_root)
        if not inherited
        else os.pathsep.join((str(src_root), inherited))
    )
    env["AUTOSPORT_STOP_AUTHORITY_PATH"] = str(authority_path)
    return env


def _run_child(
    authority_path: Path, program: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(program)],
        env=_child_env(authority_path),
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )


def _initialize_stopped(authority_path: Path) -> None:
    ExecutionStopAuthority(authority_path).initialize_stopped(
        operator_id="owner",
        reason="initial safe state",
        command_id="init-stop",
    )


def _initialize_armed(authority_path: Path) -> None:
    _initialize_stopped(authority_path)
    ExecutionStopAuthority(authority_path).arm(
        operator_id="owner",
        reason="approved",
        confirmation_id="initial-arm-confirmation",
        expected_revision=1,
        command_id="initial-arm",
    )


def test_process_kill_during_first_stop_commit_requires_explicit_safe_recovery(
    tmp_path: Path,
) -> None:
    authority_path = tmp_path / "execution-stop.jsonl"

    child = _run_child(
        authority_path,
        """
        import os
        from pathlib import Path

        from autosport.execution_stop_authority import ExecutionStopAuthority

        path = Path(os.environ["AUTOSPORT_STOP_AUTHORITY_PATH"])
        authority = ExecutionStopAuthority(path)

        def crash_before_first_anchor(*, revision: int, record_sha256: str) -> None:
            assert revision == 1
            assert len(record_sha256) == 64
            os._exit(72)

        authority._write_anchor_unlocked = crash_before_first_anchor
        authority.initialize_stopped(
            operator_id="owner",
            reason="initial safe state",
            command_id="init-before-anchor",
        )
        raise SystemExit(99)
        """,
    )

    assert child.returncode == 72, child.stderr

    restarted = ExecutionStopAuthority(authority_path)
    assert authority_path.exists()
    assert not restarted.anchor_path.exists()

    decision_before = restarted.decision()
    assert decision_before.allowed is False
    assert decision_before.mode is ExecutionAuthorityMode.STOPPED
    assert decision_before.revision is None

    with pytest.raises(
        ExecutionStopIntegrityError,
        match="journal/anchor pair is incomplete",
    ):
        restarted.current()

    recovered = restarted.recover_torn_transition()
    assert recovered.revision == 1
    assert recovered.mode is ExecutionAuthorityMode.STOPPED
    assert recovered.command_id == "init-before-anchor"
    assert restarted.anchor_path.exists()
    assert restarted.current() == recovered
    assert restarted.decision().allowed is False
    assert restarted.recover_torn_transition() == recovered


def test_process_kill_before_arm_anchor_commit_rolls_back_to_stopped(
    tmp_path: Path,
) -> None:
    authority_path = tmp_path / "execution-stop.jsonl"
    _initialize_stopped(authority_path)

    child = _run_child(
        authority_path,
        """
        import os
        from pathlib import Path

        from autosport.execution_stop_authority import ExecutionStopAuthority

        path = Path(os.environ["AUTOSPORT_STOP_AUTHORITY_PATH"])
        authority = ExecutionStopAuthority(path)

        def crash_before_anchor(*, revision: int, record_sha256: str) -> None:
            assert revision == 2
            assert len(record_sha256) == 64
            os._exit(73)

        authority._write_anchor_unlocked = crash_before_anchor
        authority.arm(
            operator_id="owner",
            reason="approved",
            confirmation_id="confirm-before-anchor",
            expected_revision=1,
            command_id="arm-before-anchor",
        )
        raise SystemExit(99)
        """,
    )

    assert child.returncode == 73, child.stderr

    restarted = ExecutionStopAuthority(authority_path)
    decision_before = restarted.decision()
    assert decision_before.allowed is False
    assert decision_before.mode is ExecutionAuthorityMode.STOPPED
    assert decision_before.revision is None

    with pytest.raises(ExecutionStopIntegrityError, match="older or newer"):
        restarted.current()

    recovered = restarted.recover_torn_transition()
    assert recovered.revision == 1
    assert recovered.mode is ExecutionAuthorityMode.STOPPED
    assert recovered.command_id == "init-stop"
    assert restarted.current() == recovered
    assert restarted.decision().allowed is False
    assert len(authority_path.read_text(encoding="utf-8").splitlines()) == 1


def test_process_kill_before_stop_anchor_commit_completes_safe_stop(
    tmp_path: Path,
) -> None:
    authority_path = tmp_path / "execution-stop.jsonl"
    _initialize_armed(authority_path)

    child = _run_child(
        authority_path,
        """
        import os
        from pathlib import Path

        from autosport.execution_stop_authority import ExecutionStopAuthority

        path = Path(os.environ["AUTOSPORT_STOP_AUTHORITY_PATH"])
        authority = ExecutionStopAuthority(path)

        def crash_before_anchor(*, revision: int, record_sha256: str) -> None:
            assert revision == 3
            assert len(record_sha256) == 64
            os._exit(75)

        authority._write_anchor_unlocked = crash_before_anchor
        authority.stop(
            operator_id="owner",
            reason="emergency stop",
            expected_revision=2,
            command_id="stop-before-anchor",
        )
        raise SystemExit(99)
        """,
    )

    assert child.returncode == 75, child.stderr

    restarted = ExecutionStopAuthority(authority_path)
    decision_before = restarted.decision()
    assert decision_before.allowed is False
    assert decision_before.mode is ExecutionAuthorityMode.STOPPED
    assert decision_before.revision is None

    with pytest.raises(ExecutionStopIntegrityError, match="older or newer"):
        restarted.current()
    with pytest.raises(ExecutionStopIntegrityError, match="older or newer"):
        restarted.assert_execution_allowed()

    recovered = restarted.recover_torn_transition()
    assert recovered.revision == 3
    assert recovered.mode is ExecutionAuthorityMode.STOPPED
    assert recovered.command_id == "stop-before-anchor"
    assert restarted.current() == recovered
    assert restarted.decision().allowed is False
    assert restarted.recover_torn_transition() == recovered


def test_process_kill_after_arm_anchor_commit_recovers_exact_durable_arm(
    tmp_path: Path,
) -> None:
    authority_path = tmp_path / "execution-stop.jsonl"
    _initialize_stopped(authority_path)

    child = _run_child(
        authority_path,
        """
        import os
        from pathlib import Path

        from autosport.execution_stop_authority import ExecutionStopAuthority

        path = Path(os.environ["AUTOSPORT_STOP_AUTHORITY_PATH"])
        authority = ExecutionStopAuthority(path)
        durable_anchor_write = authority._write_anchor_unlocked

        def commit_anchor_then_crash(*, revision: int, record_sha256: str) -> None:
            durable_anchor_write(
                revision=revision,
                record_sha256=record_sha256,
            )
            os._exit(74)

        authority._write_anchor_unlocked = commit_anchor_then_crash
        authority.arm(
            operator_id="owner",
            reason="approved",
            confirmation_id="confirm-after-anchor",
            expected_revision=1,
            command_id="arm-after-anchor",
        )
        raise SystemExit(99)
        """,
    )

    assert child.returncode == 74, child.stderr

    restarted = ExecutionStopAuthority(authority_path)
    state = restarted.current()
    assert state.revision == 2
    assert state.mode is ExecutionAuthorityMode.ARMED
    assert state.command_id == "arm-after-anchor"
    assert state.confirmation_id == "confirm-after-anchor"

    decision = restarted.decision()
    assert decision.allowed is True
    assert decision.revision == 2
    assert restarted.assert_execution_allowed() == state
