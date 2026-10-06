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
        session_id="session-progress-state-guard",
        source_id="provider-a",
        clock=lambda: _AT,
    )
    return path, state


@pytest.mark.parametrize(
    ("state_value", "error_type"),
    (
        (continuous_session.SessionState.PAUSED, continuous_session.SessionPausedError),
        (continuous_session.SessionState.STOPPED, continuous_session.SessionStoppedError),
    ),
)
def test_record_success_cannot_advance_non_running_session(
    state_value: continuous_session.SessionState,
    error_type: type[Exception],
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        path, state = _state(Path(directory))
        state.set_state(state_value, reason="OPERATOR_CONTROL")
        before = path.read_bytes()
        sidecar_path = path.with_name(f"{path.name}.operational_error.json")
        before_sidecar = sidecar_path.read_bytes()

        with pytest.raises(error_type):
            state.record_success(
                at=_AT,
                full_refresh=False,
                settlement_evidence=(),
            )

        assert path.read_bytes() == before
        assert sidecar_path.read_bytes() == before_sidecar
        durable = json.loads(before.decode("utf-8"))
        assert durable["generation"] == 1
        assert durable["cycles_completed"] == 0
        assert durable["state"] == state_value.value


@pytest.mark.parametrize(
    ("state_value", "error_type"),
    (
        (continuous_session.SessionState.PAUSED, continuous_session.SessionPausedError),
        (continuous_session.SessionState.STOPPED, continuous_session.SessionStoppedError),
    ),
)
def test_record_source_projection_cannot_publish_non_running_session(
    state_value: continuous_session.SessionState,
    error_type: type[Exception],
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        path, state = _state(Path(directory))
        state.set_state(state_value, reason="OPERATOR_CONTROL")
        before = path.read_bytes()
        sidecar_path = path.with_name(f"{path.name}.operational_error.json")
        before_sidecar = sidecar_path.read_bytes()

        with pytest.raises(error_type):
            state.record_source_projection(
                deltas=(),
                backlog=False,
            )

        assert path.read_bytes() == before
        assert sidecar_path.read_bytes() == before_sidecar
        durable = json.loads(before.decode("utf-8"))
        assert durable["generation"] == 1
        assert durable["source_state_delta_id"] is None
        assert durable["state"] == state_value.value

@pytest.mark.parametrize(
    "state_value",
    (
        continuous_session.SessionState.PAUSED,
        continuous_session.SessionState.STOPPED,
    ),
)
def test_operator_state_transition_preserves_active_same_generation_failure(
    state_value: continuous_session.SessionState,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        path, state = _state(Path(directory))
        failure = state.record_failure(code="ProviderUnavailableError")
        assert failure.generation == 0

        state.set_state(state_value)

        durable = json.loads(path.read_text(encoding="utf-8"))
        sidecar = json.loads(
            path.with_name(f"{path.name}.operational_error.json").read_text(
                encoding="utf-8"
            )
        )
        assert durable["generation"] == 1
        assert durable["state"] == state_value.value
        assert durable["last_error_code"] == "ProviderUnavailableError"
        assert sidecar["observed_generation"] == 1
        assert sidecar["observed_state"] == state_value.value
        assert sidecar["last_error_code"] is None
        assert state.snapshot().last_error_code == "ProviderUnavailableError"

