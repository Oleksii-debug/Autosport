from __future__ import annotations

import multiprocessing
import threading
from pathlib import Path

from autosport.endurance import EnduranceConfig, run_endurance


def _alive_non_daemon_threads() -> tuple[threading.Thread, ...]:
    current = threading.current_thread()
    return tuple(
        thread
        for thread in threading.enumerate()
        if thread is not current and thread.is_alive() and not thread.daemon
    )


def _alive_child_processes() -> tuple[multiprocessing.Process, ...]:
    return tuple(
        child
        for child in multiprocessing.active_children()
        if child.is_alive()
    )


def _new_identity_objects[T](
    current: tuple[T, ...],
    baseline: tuple[T, ...],
) -> tuple[T, ...]:
    """Return live objects that are not the exact objects captured at baseline."""

    return tuple(
        item
        for item in current
        if not any(item is baseline_item for baseline_item in baseline)
    )


def _thread_label(thread: threading.Thread) -> tuple[str, int | None]:
    return (thread.name, thread.ident)


def _process_label(process: multiprocessing.Process) -> tuple[str, int | None]:
    return (process.name, process.pid)


def test_repeated_endurance_runs_leave_no_new_live_threads_or_child_processes(
    tmp_path: Path,
) -> None:
    """Returned endurance runs must synchronously release owned execution resources."""

    # Keep strong references to the exact baseline objects for the whole test. Comparing
    # object identity avoids a false negative if an OS/Python identifier is reused after
    # a baseline resource exits and a leaked replacement appears with the same name/id.
    baseline_threads = _alive_non_daemon_threads()
    baseline_children = _alive_child_processes()
    config = EnduranceConfig(
        event_count=120,
        quote_keys=12,
        batch_size=30,
        restart_cycles=2,
        paper_tickets=6,
    )

    for cycle in range(3):
        report = run_endurance(tmp_path / f"cycle-{cycle}", config)

        assert report.status == "PASS", report.failures

        leaked_threads = _new_identity_objects(
            _alive_non_daemon_threads(), baseline_threads
        )
        leaked_children = _new_identity_objects(
            _alive_child_processes(), baseline_children
        )
        assert leaked_threads == (), (
            "run_endurance returned while new non-daemon threads were still alive: "
            f"{sorted(_thread_label(thread) for thread in leaked_threads)!r}"
        )
        assert leaked_children == (), (
            "run_endurance returned while new multiprocessing children were still alive: "
            f"{sorted(_process_label(child) for child in leaked_children)!r}"
        )
