"""Preflight avoids a result request that cannot meet the proposal deadline."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

import pytest

import forward_scoped_capture as capture


@pytest.mark.parametrize("age,permitted", [(-0.001, False), (0, True),
                                           (29.999, True), (30, False), (31, False)])
def test_results_requested_only_if_quote_window_remains(monkeypatch, age, permitted):
    received = datetime(2026, 11, 4, tzinfo=timezone.utc)
    calls = []
    monkeypatch.setattr(capture, "verify_session", lambda session: {
        (2, "odds"): {"received_at": received.isoformat()}})
    monkeypatch.setattr(capture, "now", lambda: received + timedelta(seconds=age))
    monkeypatch.setattr(capture, "capture_one", lambda *args: calls.append(args) or "captured")
    if permitted:
        assert capture.capture_results_with_fresh_quote(Path("session"), 2) == "captured"
        assert calls == [(Path("session"), 2, "results")]
    else:
        with pytest.raises(ValueError, match="outside the 30-second window"):
            capture.capture_results_with_fresh_quote(Path("session"), 2)
        assert calls == []


def test_missing_verified_quote_stops_before_source_call(monkeypatch):
    calls = []
    monkeypatch.setattr(capture, "verify_session", lambda session: {})
    monkeypatch.setattr(capture, "capture_one", lambda *args: calls.append(args))
    with pytest.raises(ValueError, match="quote receipt is missing"):
        capture.capture_results_with_fresh_quote(Path("session"), 2)
    assert calls == []


def test_results2_stage_stops_before_request_when_quote_expired(monkeypatch, tmp_path):
    received = datetime(2026, 11, 4, tzinfo=timezone.utc)
    calls = []
    monkeypatch.setattr(sys, "argv", ["forward_scoped_capture", "results2", str(tmp_path),
                                     str(tmp_path), "--base-head", "unused", "--history-head", "h",
                                     "--scope-sha", "s", "--date", "2026-11-04"])
    monkeypatch.setattr(capture, "load_scope", lambda *args: ({}, set()))
    monkeypatch.setattr(capture, "verify_session", lambda session: {
        (2, "odds"): {"received_at": received.isoformat()}})
    monkeypatch.setattr(capture, "now", lambda: received + timedelta(seconds=31))
    monkeypatch.setattr(capture, "capture_one", lambda *args: calls.append(args))
    with pytest.raises(ValueError, match="outside the 30-second window"):
        capture.main()
    assert calls == []
