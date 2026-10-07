from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from autosport import continuous_session


_AT = "2026-10-06T00:00:00+00:00"


def _delta() -> continuous_session.CollectorDelta:
    return continuous_session.CollectorDelta(
        schema_version=1,
        delta_id="delta-projection-fence-1",
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


def test_generation_neutral_projection_fences_stale_failure_until_bounded_refresh() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "continuous_session.json"
        stale = continuous_session._ContinuousSessionState(
            path,
            session_id="session-projection-identity-fence",
            source_id="provider-a",
            clock=lambda: _AT,
        )
        current = continuous_session._ContinuousSessionState(
            path,
            session_id="session-projection-identity-fence",
            source_id="provider-a",
            clock=lambda: _AT,
        )

        current.record_source_projection(deltas=(_delta(),), backlog=False)

        current_snapshot = current.snapshot()
        assert current_snapshot.cycles_completed == 0
        assert current._generation == 0
        assert current_snapshot.source_state_delta_id == "delta-projection-fence-1"

        with pytest.raises(
            continuous_session.ContinuousSessionError,
            match="canonical checkpoint changed",
        ):
            stale.record_failure(code="STALE_PRE_PROJECTION_FAILURE")

        # bounded_state() is the canonical external-change edge: it performs the
        # full durable refresh once, adopts the new checkpoint identity, and then
        # returns to bounded steady-state checks.
        assert stale.bounded_state() is continuous_session.SessionState.RUNNING

        publication = stale.record_failure(code="POST_REFRESH_FAILURE")
        assert publication.generation == 0
        assert publication.cycles_completed == 0
        assert publication.last_error_code == "POST_REFRESH_FAILURE"

        reopened = continuous_session._ContinuousSessionState(
            path,
            session_id="session-projection-identity-fence",
            source_id="provider-a",
            clock=lambda: _AT,
        )
        snapshot = reopened.snapshot()
        assert snapshot.last_error_code == "POST_REFRESH_FAILURE"
        assert snapshot.source_state_delta_id == "delta-projection-fence-1"
