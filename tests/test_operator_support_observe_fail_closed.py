"""The diagnostics channel is never control-plane authority."""
from datetime import datetime, timezone

import pytest

from autosport.operator_support import OperatorSupportTrace, verify_report_pair


@pytest.mark.parametrize("failure", ("clock_exception", "naive_clock", "queue_exception"))
def test_diagnostics_observe_fault_does_not_break_operator_input(
    tmp_path, monkeypatch, failure,
):
    now = datetime(2026, 10, 17, tzinfo=timezone.utc)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    trace = OperatorSupportTrace(workspace, auto_start=False, clock=lambda: now)
    original_clock = trace._clock
    try:
        trace.heartbeat()
        if failure == "clock_exception":
            def broken_clock():
                raise RuntimeError("synthetic clock failure")
            trace._clock = broken_clock
        elif failure == "naive_clock":
            trace._clock = lambda: datetime(2026, 10, 17)
        else:
            def broken_queue(_item):
                raise RuntimeError("synthetic instrumentation failure")
            monkeypatch.setattr(trace._pending, "put_nowait", broken_queue)

        # Observation is best-effort. None of the above failures can escape
        # to the UI or emergency controls and be misread as an action failure.
        trace.observe(
            component="runtime", action="product.tick", status="active", cycle=1,
        )
        trace._clock = original_clock
        diagnostic, agent_path = trace.export_reports(
            tmp_path, selected_strategy="observe-only-v1"
        )
        import json
        d = json.loads(diagnostic.read_text(encoding="utf-8"))
        assert d["coverage"]["recorder_status"] == "RECORDER_ERROR"
        assert d["coverage"]["recorder_fault_present"] is True
        assert d["coverage"]["complete_activity_capture_proven"] is False
        assert d["activities"] == []
        assert verify_report_pair(diagnostic, agent_path) == d["report_pair_sha256"]
    finally:
        trace._clock = original_clock
        trace.close()
        # A faulty recorder deliberately does not close its uncertain writer.
        # Release the fixture's lease after verifying its fail-closed export.
        if trace._journal is not None and trace._fault is not None:
            trace._journal.close({"observation_phase": trace._phase})


def test_closed_trace_never_touches_unavailable_clock(tmp_path):
    now = datetime(2026, 10, 17, tzinfo=timezone.utc)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    trace = OperatorSupportTrace(workspace, auto_start=False, clock=lambda: now)
    trace.close()

    def unavailable_clock():
        raise RuntimeError("clock must not run after close")

    trace._clock = unavailable_clock
    trace.observe(component="runtime", action="product.tick", status="active")
