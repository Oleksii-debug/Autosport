from __future__ import annotations

from pathlib import Path

import pytest

import autosport.run_registry as run_registry
from autosport.run_registry import RunRegistry
from autosport.workspace_lock import WorkspaceEconomicLock, WorkspaceEconomicLockError


def test_generic_lock_error_with_legacy_busy_text_is_not_contention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only WorkspaceEconomicLockBusyError may enter first-open retry/re-read."""

    legacy_busy_text = "another Autosport process owns the workspace economic-writer lock"
    integrity_error = WorkspaceEconomicLockError(legacy_busy_text)

    def fail_acquire(_lock: WorkspaceEconomicLock) -> None:
        raise integrity_error

    # If the implementation wrongly classifies by text, make its bounded retry expire
    # immediately rather than sleeping. Correct typed dispatch propagates the exact
    # integrity failure before consulting either clock or retry path.
    monotonic_values = iter((0.0, 10.0))
    monkeypatch.setattr(WorkspaceEconomicLock, "acquire", fail_acquire)
    monkeypatch.setattr(run_registry.time, "monotonic", lambda: next(monotonic_values))

    with pytest.raises(WorkspaceEconomicLockError) as caught:
        RunRegistry.initialize_pristine(tmp_path / "run_registry.json")

    assert caught.value is integrity_error
    assert not (tmp_path / "run_registry.json").exists()
