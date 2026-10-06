from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from autosport import continuous_session


_AT = "2026-10-06T00:00:00+00:00"


def _state_pair(root: Path):
    path = root / "continuous_session.json"
    stale = continuous_session._ContinuousSessionState(
        path,
        session_id="session-observation-generation-fence",
        source_id="provider-a",
        clock=lambda: _AT,
    )
    current = continuous_session._ContinuousSessionState(
        path,
        session_id="session-observation-generation-fence",
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


def _advance_generation(current) -> None:
    assert (
        current.record_success(
            at=_AT,
            full_refresh=False,
            settlement_evidence=(),
        )
        == 1
    )


def test_stale_provider_unavailable_observation_cannot_overlay_newer_success() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path, stale, current = _state_pair(Path(directory))

        class Cycle:
            provider_unavailable = True
            source_id = "provider-a"
            committed_delta_ids = ()

        class Collector:
            def run_cycle(self):
                _advance_generation(current)
                return Cycle()

        coordinator = _coordinator(stale, Collector())

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="provider-unavailable observation was superseded",
        ):
            coordinator.tick()

        canonical = json.loads(path.read_text(encoding="utf-8"))
        sidecar = json.loads(
            path.with_name(f"{path.name}.operational_error.json").read_text(
                encoding="utf-8"
            )
        )
        assert canonical["generation"] == 1
        assert canonical["cycles_completed"] == 1
        assert canonical["last_error_code"] is None
        assert sidecar["observed_generation"] == 1
        assert sidecar["last_error_code"] is None


def test_stale_successful_observation_cannot_run_product_side_effects() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path, stale, current = _state_pair(Path(directory))

        class Cycle:
            provider_unavailable = False
            source_id = "provider-a"
            committed_delta_ids = ()

        class Collector:
            def run_cycle(self):
                _advance_generation(current)
                return Cycle()

        coordinator = _coordinator(stale, Collector())

        def forbidden_projection():
            raise AssertionError(
                "stale collector observation reached product side effects"
            )

        coordinator._refresh_source_state_projection = forbidden_projection

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="collector observation was superseded",
        ):
            coordinator.tick()

        canonical = json.loads(path.read_text(encoding="utf-8"))
        assert canonical["generation"] == 1
        assert canonical["cycles_completed"] == 1
        assert canonical["last_error_code"] is None


def test_source_projection_does_not_supersede_active_operational_failure() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "continuous_session.json"
        state = continuous_session._ContinuousSessionState(
            path,
            session_id="session-projection-preserves-failure",
            source_id="provider-a",
            clock=lambda: _AT,
        )
        failure = state.record_failure(code="ProviderUnavailableError")
        sidecar_path = path.with_name(f"{path.name}.operational_error.json")
        sidecar_before = sidecar_path.read_bytes()

        delta = continuous_session.CollectorDelta(
            schema_version=1,
            delta_id="delta-1",
            source_id="provider-a",
            lawful_terms_ref="terms-1",
            retention_ref="retention-1",
            stream_epoch="epoch-1",
            source_cursor="cursor-1",
            cursor_position=1,
            event_dedupe_key="event-dedupe-1",
            event_id="event-1",
            source_payload_digest="0" * 64,
            canonical_event_digest="1" * 64,
            source_observed_at=_AT,
            collector_received_at=_AT,
            collector_committed_at=_AT,
            desktop_available_at=_AT,
            gap_state=continuous_session.GapState.NONE,
            sync_state=continuous_session.SyncState.READY,
        )
        state.record_source_projection(deltas=(delta,), backlog=False)

        canonical = json.loads(path.read_text(encoding="utf-8"))
        assert canonical["generation"] == failure.generation
        assert sidecar_path.read_bytes() == sidecar_before
        snapshot = state.snapshot()
        assert snapshot.last_error_code == "ProviderUnavailableError"
        assert snapshot.source_state_delta_id == "delta-1"
