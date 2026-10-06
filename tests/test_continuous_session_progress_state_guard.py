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

def test_success_ignores_instance_shadowed_rmw_dispatch() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path, state = _state(Path(directory))

        def attacker_update(*_args: object, **_kwargs: object) -> dict[str, object]:
            raise AssertionError("instance-shadowed success RMW executed")

        state._update = attacker_update  # type: ignore[method-assign]
        assert (
            state.record_success(
                at=_AT,
                full_refresh=False,
                settlement_evidence=(),
            )
            == 1
        )

        durable = json.loads(path.read_text(encoding="utf-8"))
        assert durable["generation"] == 1
        assert durable["cycles_completed"] == 1


def test_source_projection_ignores_instance_shadowed_rmw_dispatch() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path, state = _state(Path(directory))

        def attacker_update(*_args: object, **_kwargs: object) -> dict[str, object]:
            raise AssertionError("instance-shadowed projection RMW executed")

        state._update = attacker_update  # type: ignore[method-assign]
        state.record_source_projection(deltas=(), backlog=False)

        durable = json.loads(path.read_text(encoding="utf-8"))
        assert durable["generation"] == 0
        assert durable["source_state_delta_id"] is None


@pytest.mark.parametrize("operation", ("success", "projection"))
def test_progress_publication_rejects_class_rebound_rmw_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        path, state = _state(Path(directory))
        before = path.read_bytes()

        def attacker_update(
            _self: object,
            *_args: object,
            **_kwargs: object,
        ) -> dict[str, object]:
            raise AssertionError("class-rebound progress RMW executed")

        monkeypatch.setattr(
            continuous_session._ContinuousSessionState,
            "_update",
            attacker_update,
        )

        if operation == "success":
            call = lambda: state.record_success(
                at=_AT,
                full_refresh=False,
                settlement_evidence=(),
            )
            expected = "canonical success read-modify-write authority changed"
        else:
            call = lambda: state.record_source_projection(
                deltas=(),
                backlog=False,
            )
            expected = "canonical source-projection read-modify-write authority changed"

        with pytest.raises(continuous_session.ContinuousSessionError, match=expected):
            call()

        assert path.read_bytes() == before

def test_session_rmw_ignores_instance_shadowed_reader_and_identity() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path, state = _state(Path(directory))
        before = path.read_bytes()

        def attacker_read() -> dict[str, object]:
            raise AssertionError("instance-shadowed canonical reader executed")

        def attacker_identity() -> tuple[int, int, int, int, int]:
            raise AssertionError("instance-shadowed checkpoint identity executed")

        state._read = attacker_read  # type: ignore[method-assign]
        state._checkpoint_identity_token = attacker_identity  # type: ignore[method-assign]

        updated = state._update(lambda _raw: False)

        assert updated["generation"] == 0
        assert path.read_bytes() == before


@pytest.mark.parametrize(
    "method_name",
    ("_read", "_checkpoint_identity_token"),
)
def test_session_rmw_rejects_class_rebound_reader_or_identity(
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        path, state = _state(Path(directory))
        before = path.read_bytes()

        def attacker(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("class-rebound session RMW dependency executed")

        monkeypatch.setattr(
            continuous_session._ContinuousSessionState,
            method_name,
            attacker,
        )

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="canonical session read-modify-write authority changed",
        ):
            state._update(lambda _raw: False)

        assert path.read_bytes() == before

def test_success_rejects_runtime_timestamp_validator_rebinding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        path, state = _state(Path(directory))
        before = path.read_bytes()

        def attacker_instant(_value: object, _field: str) -> object:
            raise AssertionError("runtime-rebound success timestamp validator executed")

        monkeypatch.setattr(continuous_session, "_instant", attacker_instant)

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="canonical success timestamp authority changed",
        ):
            state.record_success(
                at=_AT,
                full_refresh=False,
                settlement_evidence=(),
            )

        assert path.read_bytes() == before


def test_settlement_evidence_normalizer_rejects_runtime_timestamp_rebinding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = continuous_session.SettlementResolution(
        event_identity="event-1",
        settlement_ref="settlement-1",
        quote_outcomes={"quote-1": "win"},
        evidence_id="evidence-1",
        evidence_sha256="0" * 64,
        available_at=_AT,
    )

    def attacker_instant(_value: object, _field: str) -> object:
        raise AssertionError("runtime-rebound evidence timestamp validator executed")

    monkeypatch.setattr(continuous_session, "_instant", attacker_instant)

    with pytest.raises(
        continuous_session.ContinuousSessionError,
        match="canonical settlement evidence timestamp authority changed",
    ):
        continuous_session._ContinuousSessionState._normalized_settlement_evidence(
            evidence
        )

@pytest.mark.parametrize("authority", ("text", "delta"))
def test_source_projection_rejects_runtime_validation_rebinding(
    monkeypatch: pytest.MonkeyPatch,
    authority: str,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        path, state = _state(Path(directory))
        before = path.read_bytes()

        if authority == "text":
            def attacker_text(_value: object, _field: str) -> str:
                raise AssertionError("runtime-rebound projection text validator executed")

            monkeypatch.setattr(continuous_session, "_text", attacker_text)
        else:
            def attacker_delta_validate(_self: object) -> None:
                raise AssertionError("runtime-rebound CollectorDelta validator executed")

            monkeypatch.setattr(
                continuous_session.CollectorDelta,
                "validate",
                attacker_delta_validate,
            )

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="canonical source-projection validation authority changed",
        ):
            state.record_source_projection(deltas=(), backlog=False)

        assert path.read_bytes() == before

def test_settlement_evidence_validation_ignores_instance_shadowed_history_reader() -> None:
    with tempfile.TemporaryDirectory() as directory:
        _, state = _state(Path(directory))

        def attacker_read() -> dict[str, object]:
            raise AssertionError("instance-shadowed settlement history reader executed")

        state._read = attacker_read  # type: ignore[method-assign]
        state.validate_settlement_evidence(settlement_evidence=())


def test_settlement_evidence_validation_rejects_class_rebound_history_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        path, state = _state(Path(directory))
        before = path.read_bytes()

        def attacker_read(_self: object) -> dict[str, object]:
            raise AssertionError("class-rebound settlement history reader executed")

        monkeypatch.setattr(
            continuous_session._ContinuousSessionState,
            "_read",
            attacker_read,
        )

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="canonical settlement evidence history authority changed",
        ):
            state.validate_settlement_evidence(settlement_evidence=())

        assert path.read_bytes() == before

def test_reader_rejects_runtime_timestamp_validator_rebinding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        path, state = _state(Path(directory))
        before = path.read_bytes()

        def attacker_instant(_value: object, _field: str) -> object:
            raise AssertionError("runtime-rebound reader timestamp validator executed")

        monkeypatch.setattr(continuous_session, "_instant", attacker_instant)

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="canonical session-reader code identity changed",
        ):
            state._read()

        assert path.read_bytes() == before

@pytest.mark.parametrize("operation", ("pause", "stop", "resume"))
def test_operator_controls_ignore_instance_shadowed_set_state(
    operation: str,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        path, state = _state(Path(directory))
        coordinator = object.__new__(
            continuous_session.ContinuousSessionCoordinator
        )
        coordinator._state = state

        if operation == "resume":
            state.set_state(
                continuous_session.SessionState.PAUSED,
                reason="OPERATOR_PAUSE",
            )

        def attacker_set_state(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("instance-shadowed operator state writer executed")

        state.set_state = attacker_set_state  # type: ignore[method-assign]

        if operation == "pause":
            coordinator.pause()
            expected = continuous_session.SessionState.PAUSED
        elif operation == "stop":
            coordinator.stop("OPERATOR_STOP")
            expected = continuous_session.SessionState.STOPPED
        else:
            coordinator.resume()
            expected = continuous_session.SessionState.RUNNING

        durable = json.loads(path.read_text(encoding="utf-8"))
        assert durable["state"] == expected.value


def test_resume_does_not_use_full_snapshot_precheck() -> None:
    with tempfile.TemporaryDirectory() as directory:
        _, state = _state(Path(directory))
        state.set_state(
            continuous_session.SessionState.PAUSED,
            reason="OPERATOR_PAUSE",
        )
        coordinator = object.__new__(
            continuous_session.ContinuousSessionCoordinator
        )
        coordinator._state = state

        def attacker_snapshot() -> object:
            raise AssertionError("resume performed a full snapshot precheck")

        state.snapshot = attacker_snapshot  # type: ignore[method-assign]
        coordinator.resume()

        assert state.bounded_state() is continuous_session.SessionState.RUNNING


@pytest.mark.parametrize("method_name", ("set_state", "bounded_state"))
def test_operator_controls_reject_class_rebound_state_authority(
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        path, state = _state(Path(directory))
        if method_name == "bounded_state":
            state.set_state(
                continuous_session.SessionState.PAUSED,
                reason="OPERATOR_PAUSE",
            )
        coordinator = object.__new__(
            continuous_session.ContinuousSessionCoordinator
        )
        coordinator._state = state
        before = path.read_bytes()

        def attacker(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("class-rebound operator control authority executed")

        monkeypatch.setattr(
            continuous_session._ContinuousSessionState,
            method_name,
            attacker,
        )

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="canonical coordinator operator-control authority changed",
        ):
            if method_name == "bounded_state":
                coordinator.resume()
            else:
                coordinator.pause()

        assert path.read_bytes() == before

def test_snapshot_ignores_instance_shadowed_main_reader() -> None:
    with tempfile.TemporaryDirectory() as directory:
        _, state = _state(Path(directory))

        def attacker_read() -> dict[str, object]:
            raise AssertionError("instance-shadowed snapshot main reader executed")

        state._read = attacker_read  # type: ignore[method-assign]
        snapshot = state.snapshot()

        assert snapshot.state is continuous_session.SessionState.RUNNING
        assert snapshot.cycles_completed == 0


def test_bounded_state_ignores_instance_shadowed_main_reader_on_refresh() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _, stale = _state(root)
        current = continuous_session._ContinuousSessionState(
            root / "continuous_session.json",
            session_id="session-progress-state-guard",
            source_id="provider-a",
            clock=lambda: _AT,
        )
        current.set_state(
            continuous_session.SessionState.PAUSED,
            reason="OPERATOR_PAUSE",
        )

        def attacker_read() -> dict[str, object]:
            raise AssertionError("instance-shadowed bounded-state reader executed")

        stale._read = attacker_read  # type: ignore[method-assign]

        assert stale.bounded_state() is continuous_session.SessionState.PAUSED


def test_session_rmw_uses_canonical_predecessor_reader() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path, state = _state(Path(directory))

        def attacker_read() -> dict[str, object]:
            raise AssertionError("instance-shadowed RMW predecessor reader executed")

        state._read = attacker_read  # type: ignore[method-assign]
        updated = state._update(lambda _raw: False)

        assert updated["generation"] == 0
        assert json.loads(path.read_text(encoding="utf-8"))["generation"] == 0

def test_existing_session_bootstrap_reopens_after_reader_hardening() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        path, state = _state(root)
        state.record_failure(code="ProviderUnavailableError")

        reopened = continuous_session._ContinuousSessionState(
            path,
            session_id="session-progress-state-guard",
            source_id="provider-a",
            clock=lambda: _AT,
        )

        snapshot = reopened.snapshot()
        assert snapshot.session_id == "session-progress-state-guard"
        assert snapshot.last_error_code == "ProviderUnavailableError"

@pytest.mark.parametrize("authority", ("text", "sha256"))
def test_durable_session_parser_rejects_runtime_scalar_validator_rebinding(
    monkeypatch: pytest.MonkeyPatch,
    authority: str,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        _, state = _state(Path(directory))

        if authority == "text":
            def attacker_text(_value: object, _field: str) -> str:
                raise AssertionError("runtime-rebound durable text validator executed")

            monkeypatch.setattr(continuous_session, "_text", attacker_text)
            expected = "canonical session-reader code identity changed"
        else:
            def attacker_sha256(_value: object, _field: str) -> str:
                raise AssertionError("runtime-rebound durable hash validator executed")

            monkeypatch.setattr(continuous_session, "_sha256", attacker_sha256)
            expected = "canonical settlement evidence parser authority changed"

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match=expected,
        ):
            state.snapshot()

def test_tick_ignores_instance_shadowed_running_precheck() -> None:
    with tempfile.TemporaryDirectory() as directory:
        _, state = _state(Path(directory))
        coordinator = object.__new__(
            continuous_session.ContinuousSessionCoordinator
        )
        coordinator._state = state
        coordinator.clock = lambda: _AT

        def attacker_require_running() -> None:
            raise AssertionError("instance-shadowed tick running precheck executed")

        coordinator._require_running = attacker_require_running  # type: ignore[method-assign]

        class Collector:
            def run_cycle(self):
                raise RuntimeError("collector reached")

        coordinator.collector = Collector()

        with pytest.raises(RuntimeError, match="collector reached"):
            coordinator.tick()


def test_tick_rejects_runtime_causal_time_validator_rebinding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        _, state = _state(Path(directory))
        coordinator = object.__new__(
            continuous_session.ContinuousSessionCoordinator
        )
        coordinator._state = state
        coordinator.clock = lambda: _AT

        class Collector:
            def run_cycle(self):
                raise AssertionError("collector ran after causal-time authority changed")

        coordinator.collector = Collector()

        def attacker_instant(_value: object, _field: str) -> object:
            raise AssertionError("runtime-rebound tick timestamp validator executed")

        monkeypatch.setattr(continuous_session, "_instant", attacker_instant)

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="canonical coordinator running-fence authority changed",
        ):
            coordinator.tick()

def _status_coordinator(
    state: continuous_session._ContinuousSessionState,
):
    coordinator = object.__new__(
        continuous_session.ContinuousSessionCoordinator
    )
    coordinator._state = state
    coordinator.collector = object.__new__(
        continuous_session.HeadlessCollectorService
    )
    coordinator.collector._state = type(
        "CollectorStateStub",
        (),
        {
            "snapshot": lambda _self: {
                "last_success_at": None,
                "last_error_code": None,
            }
        },
    )()
    coordinator.invalidation_buffer = type(
        "InvalidationBufferStub",
        (),
        {
            "pending_count": 0,
            "full_refresh_required": False,
        },
    )()
    return coordinator


def test_status_ignores_instance_shadowed_session_snapshot() -> None:
    with tempfile.TemporaryDirectory() as directory:
        _, state = _state(Path(directory))
        coordinator = _status_coordinator(state)

        def attacker_snapshot() -> object:
            raise AssertionError("instance-shadowed session status snapshot executed")

        state.snapshot = attacker_snapshot  # type: ignore[method-assign]

        status = coordinator.status()
        assert status.state is continuous_session.SessionState.RUNNING


def test_status_ignores_instance_shadowed_collector_status() -> None:
    with tempfile.TemporaryDirectory() as directory:
        _, state = _state(Path(directory))
        coordinator = _status_coordinator(state)

        def attacker_status() -> dict[str, object]:
            raise AssertionError("instance-shadowed collector status executed")

        coordinator.collector.status = attacker_status  # type: ignore[method-assign]

        status = coordinator.status()
        assert status.source_last_error_code is None


@pytest.mark.parametrize("authority", ("session", "collector", "replace"))
def test_status_rejects_rebound_truth_authority(
    monkeypatch: pytest.MonkeyPatch,
    authority: str,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        _, state = _state(Path(directory))
        coordinator = _status_coordinator(state)

        if authority == "session":
            def attacker_snapshot(_self: object) -> object:
                raise AssertionError("class-rebound session snapshot executed")
            monkeypatch.setattr(
                continuous_session._ContinuousSessionState,
                "snapshot",
                attacker_snapshot,
            )
        elif authority == "collector":
            def attacker_status(_self: object) -> dict[str, object]:
                raise AssertionError("class-rebound collector status executed")
            monkeypatch.setattr(
                continuous_session.HeadlessCollectorService,
                "status",
                attacker_status,
            )
        else:
            def attacker_replace(*_args: object, **_kwargs: object) -> object:
                raise AssertionError("runtime-rebound status replace executed")
            monkeypatch.setattr(continuous_session, "replace", attacker_replace)

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="canonical coordinator status authority changed",
        ):
            coordinator.status()

def test_source_projection_refresh_ignores_instance_shadowed_state_dispatch() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _, state = _state(root)
        coordinator = object.__new__(
            continuous_session.ContinuousSessionCoordinator
        )
        coordinator._state = state
        coordinator.collector = type(
            "CollectorStub",
            (),
            {
                "source_id": "provider-a",
                "config": type("ConfigStub", (), {"max_items": 1})(),
                "delta_store": type(
                    "DeltaStoreStub",
                    (),
                    {
                        "deltas_after_commit": lambda _self, **_kwargs: (),
                    },
                )(),
            },
        )()

        def attacker_snapshot() -> object:
            raise AssertionError("instance-shadowed projection snapshot executed")

        def attacker_projection(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("instance-shadowed projection publisher executed")

        state.snapshot = attacker_snapshot  # type: ignore[method-assign]
        state.record_source_projection = attacker_projection  # type: ignore[method-assign]

        snapshot = coordinator._refresh_source_state_projection()
        assert snapshot.source_state_delta_id is None


@pytest.mark.parametrize("method_name", ("snapshot", "record_source_projection"))
def test_source_projection_refresh_rejects_class_rebound_state_authority(
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _, state = _state(root)
        coordinator = object.__new__(
            continuous_session.ContinuousSessionCoordinator
        )
        coordinator._state = state
        coordinator.collector = type(
            "CollectorStub",
            (),
            {
                "source_id": "provider-a",
                "config": type("ConfigStub", (), {"max_items": 1})(),
                "delta_store": type(
                    "DeltaStoreStub",
                    (),
                    {
                        "deltas_after_commit": lambda _self, **_kwargs: (),
                    },
                )(),
            },
        )()

        def attacker(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("class-rebound projection state authority executed")

        monkeypatch.setattr(
            continuous_session._ContinuousSessionState,
            method_name,
            attacker,
        )

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="canonical source-projection coordinator authority changed",
        ):
            coordinator._refresh_source_state_projection()



def test_tick_ignores_instance_shadowed_failure_publication_on_provider_unavailable() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        path, state = _state(root)
        coordinator = object.__new__(
            continuous_session.ContinuousSessionCoordinator
        )
        coordinator._state = state
        coordinator.clock = lambda: _AT

        class ProviderUnavailableCycle:
            provider_unavailable = True
            source_id = "provider-a"
            committed_delta_ids = ()

        class Collector:
            def run_cycle(self):
                return ProviderUnavailableCycle()

        coordinator.collector = Collector()
        coordinator.invalidation_buffer = type(
            "InvalidationBufferStub",
            (),
            {"pending_count": 0, "full_refresh_required": False},
        )()

        def attacker_record_failure(*, code: str):
            raise AssertionError(
                f"instance-shadowed failure publisher executed for {code}"
            )

        state.record_failure = attacker_record_failure  # type: ignore[method-assign]

        result = coordinator.tick()

        assert result.source_provider_unavailable is True
        sidecar = json.loads(
            path.with_name(f"{path.name}.operational_error.json").read_text(
                encoding="utf-8"
            )
        )
        assert sidecar["last_error_code"] == "ProviderUnavailableError"


def test_tick_ignores_instance_shadowed_failure_publication_on_collector_exception() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        path, state = _state(root)
        coordinator = object.__new__(
            continuous_session.ContinuousSessionCoordinator
        )
        coordinator._state = state
        coordinator.clock = lambda: _AT

        class Collector:
            def run_cycle(self):
                raise RuntimeError("collector failure")

        coordinator.collector = Collector()

        def attacker_record_failure(*, code: str):
            raise AssertionError(
                f"instance-shadowed failure publisher executed for {code}"
            )

        state.record_failure = attacker_record_failure  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="collector failure"):
            coordinator.tick()

        sidecar = json.loads(
            path.with_name(f"{path.name}.operational_error.json").read_text(
                encoding="utf-8"
            )
        )
        assert sidecar["last_error_code"] == "RuntimeError"


def test_tick_rejects_class_rebound_failure_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        _, state = _state(Path(directory))
        coordinator = object.__new__(
            continuous_session.ContinuousSessionCoordinator
        )
        coordinator._state = state
        coordinator.clock = lambda: _AT

        class Collector:
            def run_cycle(self):
                raise AssertionError(
                    "collector ran after failure publication authority changed"
                )

        coordinator.collector = Collector()

        def attacker_record_failure(
            _self: continuous_session._ContinuousSessionState,
            *,
            code: str,
        ) -> object:
            raise AssertionError(
                f"class-rebound failure publisher executed for {code}"
            )

        monkeypatch.setattr(
            continuous_session._ContinuousSessionState,
            "record_failure",
            attacker_record_failure,
        )

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="canonical coordinator running-fence authority changed",
        ):
            coordinator.tick()
