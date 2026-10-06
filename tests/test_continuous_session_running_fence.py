from __future__ import annotations

import json
import tempfile
from pathlib import Path

from autosport import continuous_session


_AT = "2026-10-06T00:00:00+00:00"


def _state_pair(root: Path):
    path = root / "continuous_session.json"
    stale = continuous_session._ContinuousSessionState(
        path,
        session_id="session-running-fence",
        source_id="provider-a",
        clock=lambda: _AT,
    )
    current = continuous_session._ContinuousSessionState(
        path,
        session_id="session-running-fence",
        source_id="provider-a",
        clock=lambda: _AT,
    )
    return path, stale, current


def _coordinator(state, collector):
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)
    coordinator._state = state
    coordinator.collector = collector
    coordinator.clock = lambda: _AT
    return coordinator


def test_external_pause_during_collector_blocks_normal_product_side_effects() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _, stale, current = _state_pair(root)

        class Cycle:
            provider_unavailable = False

        class Collector:
            def run_cycle(self):
                current.set_state(
                    continuous_session.SessionState.PAUSED,
                    reason="OPERATOR_PAUSE",
                )
                return Cycle()

        coordinator = _coordinator(stale, Collector())

        def forbidden_projection():
            raise AssertionError("normal product side effects ran after durable pause")

        coordinator._refresh_source_state_projection = forbidden_projection

        try:
            coordinator.tick()
        except continuous_session.SessionPausedError:
            pass
        else:
            raise AssertionError("tick ignored durable pause won during observation")


def test_external_pause_during_provider_unavailable_observation_blocks_failure_write() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        path, stale, current = _state_pair(root)

        class Cycle:
            provider_unavailable = True

        class Collector:
            def run_cycle(self):
                current.set_state(
                    continuous_session.SessionState.PAUSED,
                    reason="OPERATOR_PAUSE",
                )
                return Cycle()

        coordinator = _coordinator(stale, Collector())

        try:
            coordinator.tick()
        except continuous_session.SessionPausedError:
            pass
        else:
            raise AssertionError("provider failure publication crossed durable pause")

        canonical = json.loads(path.read_text(encoding="utf-8"))
        sidecar = json.loads(
            path.with_name(f"{path.name}.operational_error.json").read_text(
                encoding="utf-8"
            )
        )
        assert canonical["state"] == "PAUSED"
        assert canonical["last_error_code"] == "OPERATOR_PAUSE"
        assert sidecar["observed_generation"] == canonical["generation"]
        assert sidecar["last_error_code"] is None


def test_collector_exception_is_not_masked_when_operator_pause_wins_error_race() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        path, stale, current = _state_pair(root)

        class Collector:
            def run_cycle(self):
                current.set_state(
                    continuous_session.SessionState.PAUSED,
                    reason="OPERATOR_PAUSE",
                )
                raise ValueError("provider decode failed")

        coordinator = _coordinator(stale, Collector())

        try:
            coordinator.tick()
        except ValueError as exc:
            assert str(exc) == "provider decode failed"
        else:
            raise AssertionError("collector exception was masked by pause handling")

        canonical = json.loads(path.read_text(encoding="utf-8"))
        assert canonical["state"] == "PAUSED"
        assert canonical["last_error_code"] == "OPERATOR_PAUSE"

def test_require_running_ignores_instance_shadowed_bounded_state() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _, stale, current = _state_pair(root)
        current.set_state(
            continuous_session.SessionState.PAUSED,
            reason="OPERATOR_PAUSE",
        )
        stale.bounded_state = lambda: continuous_session.SessionState.RUNNING

        coordinator = _coordinator(stale, object())

        try:
            coordinator._require_running()
        except continuous_session.SessionPausedError:
            pass
        else:
            raise AssertionError("instance-shadowed bounded_state bypassed durable pause")


def test_tick_ignores_instance_shadowed_running_fence() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _, stale, current = _state_pair(root)

        class Cycle:
            provider_unavailable = False

        class Collector:
            def run_cycle(self):
                current.set_state(
                    continuous_session.SessionState.PAUSED,
                    reason="OPERATOR_PAUSE",
                )
                return Cycle()

        class FakeFence:
            def __enter__(self):
                return None

            def __exit__(self, *_args):
                return None

        stale.running_fence = lambda: FakeFence()
        coordinator = _coordinator(stale, Collector())

        try:
            coordinator.tick()
        except continuous_session.SessionPausedError:
            pass
        else:
            raise AssertionError("instance-shadowed running_fence bypassed durable pause")


def test_tick_rejects_class_rebound_running_fence(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _, state, _ = _state_pair(root)

        canonical = continuous_session._ContinuousSessionState.running_fence

        def attacker_fence(_self):
            raise AssertionError("class-rebound running fence executed")

        monkeypatch.setattr(
            continuous_session._ContinuousSessionState,
            "running_fence",
            attacker_fence,
        )
        coordinator = _coordinator(state, object())

        try:
            coordinator.tick()
        except continuous_session.ContinuousSessionError as exc:
            assert "running-fence authority changed" in str(exc)
        else:
            raise AssertionError("class-rebound running fence was accepted")

        assert canonical is not attacker_fence

