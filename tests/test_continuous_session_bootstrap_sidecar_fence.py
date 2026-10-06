from __future__ import annotations

import tempfile
import threading
from pathlib import Path

from autosport import continuous_session


_AT = "2026-10-06T00:00:00+00:00"


def test_bootstrap_sidecar_observation_holds_session_lock(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "continuous_session.json"
        current = continuous_session._ContinuousSessionState(
            path,
            session_id="session-bootstrap-sidecar-fence",
            source_id="provider-a",
            clock=lambda: _AT,
        )

        original_present = continuous_session._ContinuousSessionState._error_checkpoint_present
        writer_started = threading.Event()
        writer_finished = threading.Event()
        writer_thread: threading.Thread | None = None

        def writer() -> None:
            writer_started.set()
            current.set_state(
                continuous_session.SessionState.PAUSED,
                reason="OPERATOR_PAUSE",
            )
            writer_finished.set()

        def fenced_present(self) -> bool:
            nonlocal writer_thread
            if writer_thread is None:
                writer_thread = threading.Thread(target=writer)
                writer_thread.start()
                assert writer_started.wait(timeout=1.0)
                writer_thread.join(timeout=0.1)
                assert writer_thread.is_alive(), (
                    "concurrent canonical writer escaped the bootstrap lock "
                    "before sidecar observation completed"
                )
            return original_present(self)

        monkeypatch.setattr(
            continuous_session._ContinuousSessionState,
            "_error_checkpoint_present",
            fenced_present,
        )

        reopened = continuous_session._ContinuousSessionState(
            path,
            session_id="session-bootstrap-sidecar-fence",
            source_id="provider-a",
            clock=lambda: _AT,
        )

        assert writer_thread is not None
        writer_thread.join(timeout=1.0)
        assert writer_finished.is_set()
        assert not writer_thread.is_alive()
        assert reopened.bounded_state() is continuous_session.SessionState.PAUSED
