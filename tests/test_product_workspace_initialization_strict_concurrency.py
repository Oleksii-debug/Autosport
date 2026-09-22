from __future__ import annotations

import multiprocessing
from pathlib import Path

import pytest

from autosport.product_workspace_initialization import initialize_product_workspace


def _strict_first_run_worker(
    workspace: str,
    authority_root: str,
    start,
    results,
) -> None:
    if not start.wait(20):
        results.put(("error", "start-barrier-timeout", ""))
        return
    try:
        initialized = initialize_product_workspace(
            Path(workspace),
            authority_root=Path(authority_root),
        )
    except BaseException as exc:
        results.put(("error", type(exc).__name__, str(exc)))
        return
    results.put(("ok", initialized.workspace_instance_id, ""))


def _join_worker(process: multiprocessing.Process) -> None:
    process.join(30)
    if process.is_alive():
        process.terminate()
        process.join(10)
        pytest.fail("concurrent first-run initializer did not terminate")
    assert process.exitcode == 0


@pytest.mark.parametrize("round_index", range(3))
def test_two_pristine_first_runs_both_converge_without_expected_error(
    tmp_path: Path,
    round_index: int,
) -> None:
    workspace = tmp_path / f"workspace-{round_index}"
    authority_root = tmp_path / f"machine-state-{round_index}"
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    workers = [
        context.Process(
            target=_strict_first_run_worker,
            args=(str(workspace), str(authority_root), start, results),
        )
        for _ in range(2)
    ]

    for worker in workers:
        worker.start()
    start.set()
    for worker in workers:
        _join_worker(worker)

    messages = [results.get(timeout=5), results.get(timeout=5)]

    assert all(message[0] == "ok" for message in messages), messages
    ids = [message[1] for message in messages]
    assert len(set(ids)) == 1

    reopened = initialize_product_workspace(workspace, authority_root=authority_root)
    assert reopened.workspace_instance_id == ids[0]
