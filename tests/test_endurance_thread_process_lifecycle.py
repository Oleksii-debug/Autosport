from __future__ import annotations

import multiprocessing
import threading
from pathlib import Path

from autosport.endurance import EnduranceConfig, run_endurance


def _alive_non_daemon_threads() -> set[tuple[str, int | None]]:
    current = threading.current_thread()
    return {
        (thread.name, thread.ident)
        for thread in threading.enumerate()
        if thread is not current and thread.is_alive() and not thread.daemon
    }


def _alive_child_processes() -> set[tuple[str, int | None]]:
    return {
        (child.name, child.pid)
        for child in multiprocessing.active_children()
        if child.is_alive()
    }


def test_repeated_endurance_runs_leave_no_new_live_threads_or_child_processes(
    tmp_path: Path,
) -> None:
    """Returned endurance runs must synchronously release owned execution resources."""

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

        leaked_threads = _alive_non_daemon_threads() - baseline_threads
        leaked_children = _alive_child_processes() - baseline_children
        assert leaked_threads == set(), (
            "run_endurance returned while new non-daemon threads were still alive: "
            f"{sorted(leaked_threads)!r}"
        )
        assert leaked_children == set(), (
            "run_endurance returned while new multiprocessing children were still alive: "
            f"{sorted(leaked_children)!r}"
        )
