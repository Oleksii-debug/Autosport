from __future__ import annotations

import multiprocessing
from pathlib import Path

import pytest

from autosport.workspace_lock import (
    WorkspaceEconomicLock,
    WorkspaceEconomicLockBusyError,
    WorkspaceInteractiveLock,
)


def _hold_interactive_lock(workspace: str, ready, release) -> None:
    with WorkspaceInteractiveLock(workspace):
        ready.set()
        if not release.wait(20):
            raise RuntimeError("interactive lock holder timed out")


def test_interactive_lock_uses_distinct_persistent_authority_path(tmp_path: Path) -> None:
    interactive = WorkspaceInteractiveLock(tmp_path)
    economic = WorkspaceEconomicLock(tmp_path)

    assert interactive.path == tmp_path / ".interactive-run.lock"
    assert economic.path == tmp_path / ".economic-run.lock"
    assert interactive.path != economic.path

    with interactive:
        with economic:
            assert interactive.path.is_file()
            assert economic.path.is_file()

    with WorkspaceInteractiveLock(tmp_path):
        pass
    with WorkspaceEconomicLock(tmp_path):
        pass


def test_second_process_is_rejected_without_reserving_economic_writer_lock(
    tmp_path: Path,
) -> None:
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    process = context.Process(
        target=_hold_interactive_lock,
        args=(str(tmp_path), ready, release),
    )
    process.start()
    try:
        assert ready.wait(20), "child did not acquire interactive workspace lock"

        with pytest.raises(
            WorkspaceEconomicLockBusyError,
            match="another Autosport process owns",
        ):
            WorkspaceInteractiveLock(tmp_path).acquire()

        with WorkspaceEconomicLock(tmp_path):
            pass
    finally:
        release.set()
        process.join(20)
        if process.is_alive():
            process.terminate()
            process.join(10)
            pytest.fail("interactive lock-holder process did not exit")

    assert process.exitcode == 0
    with WorkspaceInteractiveLock(tmp_path):
        pass
