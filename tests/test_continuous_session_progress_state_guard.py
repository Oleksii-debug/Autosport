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
    ("state_value", "error_type"),
    (
        (continuous_session.SessionState.PAUSED, continuous_session.SessionPausedError),
        (continuous_session.SessionState.STOPPED, continuous_session.SessionStoppedError),
    ),
)
def test_record_failure_cannot_publish_non_running_session(
    state_value: continuous_session.SessionState,
    error_type: type[Exception],
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        path, state = _state(Path(directory))
        state.set_state(state_value, reason="OPERATOR_CONTROL")
        sidecar_path = path.with_name(f"{path.name}.operational_error.json")
        canonical_before = path.read_bytes()
        sidecar_before = sidecar_path.read_bytes()

        with pytest.raises(error_type):
            state.record_failure(code="ProviderUnavailableError")

        assert path.read_bytes() == canonical_before
        assert sidecar_path.read_bytes() == sidecar_before
        assert state.snapshot().last_error_code == "OPERATOR_CONTROL"


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

def test_operator_state_transition_rejects_same_generation_failure_marker_conflict() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path, state = _state(Path(directory))
        state.record_failure(code="ProviderUnavailableError")
        sidecar_path = path.with_name(f"{path.name}.operational_error.json")
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        sidecar["observed_cycles_completed"] = 1
        sidecar_path.write_text(
            json.dumps(sidecar, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        canonical_before = path.read_bytes()
        sidecar_before = sidecar_path.read_bytes()

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="same-generation operational error checkpoint markers conflict",
        ):
            state.set_state(continuous_session.SessionState.PAUSED)

        assert path.read_bytes() == canonical_before
        assert sidecar_path.read_bytes() == sidecar_before


def test_operator_state_transition_does_not_resurrect_stale_failure_overlay() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path, state = _state(Path(directory))
        state.record_failure(code="ProviderUnavailableError")
        stale_sidecar = json.loads(
            path.with_name(f"{path.name}.operational_error.json").read_text(
                encoding="utf-8"
            )
        )
        assert (
            state.record_success(
                at=_AT,
                full_refresh=False,
                settlement_evidence=(),
            )
            == 1
        )
        sidecar_path = path.with_name(f"{path.name}.operational_error.json")
        sidecar_path.write_text(
            json.dumps(stale_sidecar, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        state.set_state(continuous_session.SessionState.PAUSED)

        durable = json.loads(path.read_text(encoding="utf-8"))
        assert durable["generation"] == 2
        assert durable["state"] == continuous_session.SessionState.PAUSED.value
        assert durable["last_error_code"] is None
        assert state.snapshot().last_error_code is None


@pytest.mark.parametrize(
    ("method_name", "replacement"),
    (
        ("_error_checkpoint_present", lambda self: False),
        ("_read_error_checkpoint", lambda self: {}),
    ),
)
def test_record_failure_rejects_rebound_checkpoint_reader_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
    replacement: object,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        path, state = _state(Path(directory))
        sidecar_path = path.with_name(f"{path.name}.operational_error.json")
        canonical_before = path.read_bytes()
        sidecar_before = sidecar_path.read_bytes() if sidecar_path.exists() else None

        monkeypatch.setattr(
            continuous_session._ContinuousSessionState,
            method_name,
            replacement,
        )

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="canonical failure publication lock authority changed",
        ):
            state.record_failure(code="ProviderUnavailableError")

        assert path.read_bytes() == canonical_before
        if sidecar_before is None:
            assert not sidecar_path.exists()
        else:
            assert sidecar_path.read_bytes() == sidecar_before


@pytest.mark.parametrize(
    ("method_name", "replacement"),
    (
        ("_error_checkpoint_present", lambda self: False),
        ("_read_error_checkpoint", lambda self: {}),
    ),
)
def test_snapshot_rejects_rebound_checkpoint_reader_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
    replacement: object,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        _path, state = _state(Path(directory))
        state.record_failure(code="ProviderUnavailableError")

        monkeypatch.setattr(
            continuous_session._ContinuousSessionState,
            method_name,
            replacement,
        )

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="canonical operational-checkpoint snapshot authority changed",
        ):
            state.snapshot()


@pytest.mark.parametrize(
    ("method_name", "replacement"),
    (
        ("_read_error_checkpoint_bytes", lambda self, **kwargs: b"{}"),
        ("_bounded_descriptor_read", lambda descriptor, limit: b"{}"),
        ("_file_identity", lambda info: (0, 0)),
    ),
)
def test_checkpoint_reader_rejects_rebound_nested_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
    replacement: object,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        _path, state = _state(Path(directory))
        state.record_failure(code="ProviderUnavailableError")

        monkeypatch.setattr(
            continuous_session._ContinuousSessionState,
            method_name,
            replacement,
        )

        with pytest.raises(continuous_session.ContinuousSessionError):
            state._read_error_checkpoint()

def test_state_transition_ignores_instance_shadowed_rmw_dispatch() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path, state = _state(Path(directory))

        def attacker_update(*_args: object, **_kwargs: object) -> dict[str, object]:
            raise AssertionError("instance-shadowed state RMW executed")

        state._update = attacker_update  # type: ignore[method-assign]
        state.set_state(
            continuous_session.SessionState.PAUSED,
            reason="OPERATOR_PAUSE",
        )

        durable = json.loads(path.read_text(encoding="utf-8"))
        assert durable["generation"] == 1
        assert durable["state"] == continuous_session.SessionState.PAUSED.value
        assert durable["last_error_code"] == "OPERATOR_PAUSE"


def test_state_transition_rejects_class_rebound_rmw_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        path, state = _state(Path(directory))
        before = path.read_bytes()

        def attacker_update(
            _self: object,
            *_args: object,
            **_kwargs: object,
        ) -> dict[str, object]:
            raise AssertionError("class-rebound state RMW executed")

        monkeypatch.setattr(
            continuous_session._ContinuousSessionState,
            "_update",
            attacker_update,
        )

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="canonical state-transition error authority changed",
        ):
            state.set_state(continuous_session.SessionState.PAUSED)

        assert path.read_bytes() == before


def test_state_transition_rejects_runtime_reason_validator_rebinding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        path, state = _state(Path(directory))
        before = path.read_bytes()

        def attacker_text(_value: object, _field: str) -> str:
            raise AssertionError("runtime-rebound state reason validator executed")

        monkeypatch.setattr(continuous_session, "_text", attacker_text)

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="canonical state-transition error authority changed",
        ):
            state.set_state(
                continuous_session.SessionState.PAUSED,
                reason="OPERATOR_PAUSE",
            )

        assert path.read_bytes() == before

