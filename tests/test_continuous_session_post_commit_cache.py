from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from autosport import continuous_session


_AT = "2026-10-06T00:00:00+00:00"


def test_caught_cleanup_failure_keeps_instance_generation_aligned_for_next_failure() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        path = root / "continuous_session.json"
        state = continuous_session._ContinuousSessionState(
            path,
            session_id="session-post-commit-cache",
            source_id="provider-a",
            clock=lambda: _AT,
        )
        state.record_failure(code="PRE_TRANSITION_FAILURE")

        original_write_error = state._write_error_checkpoint

        def fail_cleanup(code: str | None) -> None:
            if code is None:
                raise RuntimeError("simulated post-commit cleanup failure")
            original_write_error(code)

        with patch.object(state, "_write_error_checkpoint", fail_cleanup):
            try:
                state.set_state(
                    continuous_session.SessionState.PAUSED,
                    reason="OPERATOR_PAUSE",
                )
            except RuntimeError as exc:
                assert "post-commit cleanup failure" in str(exc)
            else:
                raise AssertionError("cleanup failure was not simulated")

        canonical = json.loads(path.read_text(encoding="utf-8"))
        assert canonical["state"] == "PAUSED"
        assert canonical["generation"] == 1

        publication = state.record_failure(code="AFTER_CLEANUP_FAILURE")
        assert publication.state is continuous_session.SessionState.PAUSED
        assert publication.generation == canonical["generation"]

        sidecar = json.loads(
            path.with_name(f"{path.name}.operational_error.json").read_text(
                encoding="utf-8"
            )
        )
        assert sidecar["observed_generation"] == canonical["generation"]
        assert sidecar["observed_state"] == "PAUSED"
        assert sidecar["last_error_code"] == "AFTER_CLEANUP_FAILURE"

        snapshot = state.snapshot()
        assert snapshot.state is continuous_session.SessionState.PAUSED
        assert snapshot.last_error_code == "AFTER_CLEANUP_FAILURE"
