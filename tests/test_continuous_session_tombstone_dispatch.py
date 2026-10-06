from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from autosport import continuous_session


_AT = "2026-10-06T00:00:00+00:00"


def _state(root: Path) -> tuple[Path, continuous_session._ContinuousSessionState]:
    path = root / "continuous_session.json"
    state = continuous_session._ContinuousSessionState(
        path,
        session_id="session-tombstone-dispatch",
        source_id="provider-a",
        clock=lambda: _AT,
    )
    return path, state


@pytest.mark.parametrize("operation", ("pause", "success"))
def test_progress_commit_rejects_class_rebound_tombstone_writer(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        path, state = _state(Path(directory))
        before = path.read_bytes()

        def attacker_writer(_self: object, _code: object) -> None:
            raise AssertionError("class-rebound tombstone writer executed")

        monkeypatch.setattr(
            continuous_session._ContinuousSessionState,
            "_write_error_checkpoint",
            attacker_writer,
        )

        with pytest.raises(continuous_session.ContinuousSessionError):
            if operation == "pause":
                state.set_state(continuous_session.SessionState.PAUSED)
            else:
                state.record_success(
                    at=_AT,
                    full_refresh=False,
                    settlement_evidence=(),
                )

        assert path.read_bytes() == before
        sidecar = path.with_name(f"{path.name}.operational_error.json")
        assert not sidecar.exists()


@pytest.mark.parametrize("operation", ("pause", "success"))
def test_progress_commit_ignores_instance_shadowed_tombstone_writer(
    operation: str,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        path, state = _state(Path(directory))

        def attacker_writer(_code: object) -> None:
            raise AssertionError("instance-shadowed tombstone writer executed")

        state._write_error_checkpoint = attacker_writer  # type: ignore[method-assign]

        if operation == "pause":
            state.set_state(continuous_session.SessionState.PAUSED)
            expected_state = continuous_session.SessionState.PAUSED
            expected_cycles = 0
        else:
            state.record_success(
                at=_AT,
                full_refresh=False,
                settlement_evidence=(),
            )
            expected_state = continuous_session.SessionState.RUNNING
            expected_cycles = 1

        canonical = json.loads(path.read_text(encoding="utf-8"))
        sidecar = json.loads(
            path.with_name(f"{path.name}.operational_error.json").read_text(
                encoding="utf-8"
            )
        )
        assert canonical["state"] == expected_state.value
        assert canonical["cycles_completed"] == expected_cycles
        assert sidecar["observed_generation"] == canonical["generation"]
        assert sidecar["observed_state"] == canonical["state"]
        assert sidecar["last_error_code"] is None
