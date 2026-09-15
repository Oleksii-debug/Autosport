from __future__ import annotations

import json
import multiprocessing
from pathlib import Path

import pytest

from autosport.run_registry import RunRegistry, has_durable_workspace_history
from autosport.workspace_lock import WorkspaceEconomicLock


_MARKET_SHA = "a" * 64
_RESULTS_SHA = "b" * 64


def _stale_missing_first_open_worker(
    workspace: str,
    missing_observed,
    continue_after_writer_lock,
    reread_done,
    results,
) -> None:
    root = Path(workspace)
    path = root / "run_registry.json"
    try:
        # Deterministically represent the exact stale observation that used to grant
        # publication authority before any workspace lock was acquired.
        if path.exists():
            raise AssertionError("registry unexpectedly existed before stale observation")
        registry = object.__new__(RunRegistry)
        registry.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        missing_observed.set()
        if not continue_after_writer_lock.wait(20):
            raise RuntimeError("timed out waiting for writer lock")
        registry._initialize_missing_registry()
        state = registry._read()
        results.put(("reader", state["schema_version"], len(state["runs"])))
        reread_done.set()
    except BaseException as exc:
        results.put(("reader-error", type(exc).__name__, str(exc)))
        reread_done.set()
        raise


def _first_open_writer_worker(
    workspace: str,
    missing_observed,
    continue_after_writer_lock,
    reread_done,
    results,
) -> None:
    root = Path(workspace)
    path = root / "run_registry.json"
    try:
        if not missing_observed.wait(20):
            raise RuntimeError("timed out waiting for stale missing observation")
        registry = RunRegistry.initialize_pristine(path)
        with WorkspaceEconomicLock(root):
            continue_after_writer_lock.set()
            if not reread_done.wait(20):
                raise RuntimeError("stale opener did not re-read while writer owned lock")
            key = registry.begin(
                _MARKET_SHA,
                _RESULTS_SHA,
                "baseline-v1",
                "writer-run",
            )
            registry.complete(key)
        results.put(("writer", key))
    except BaseException as exc:
        results.put(("writer-error", type(exc).__name__, str(exc)))
        raise


def _simultaneous_clean_first_open_worker(workspace: str, start, results) -> None:
    try:
        if not start.wait(20):
            raise RuntimeError("timed out waiting for simultaneous first-open start")
        registry = RunRegistry.initialize_pristine(Path(workspace) / "run_registry.json")
        results.put(("ok", registry.strategy_ids()))
    except BaseException as exc:
        results.put(("error", type(exc).__name__, str(exc)))
        raise


def _join_cleanly(process: multiprocessing.Process) -> None:
    process.join(30)
    if process.is_alive():
        process.terminate()
        process.join(10)
        pytest.fail("spawned first-open worker did not exit")
    assert process.exitcode == 0


def test_stale_missing_observation_cannot_publish_over_locked_writer(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    missing_observed = context.Event()
    continue_after_writer_lock = context.Event()
    reread_done = context.Event()
    results = context.Queue()

    reader = context.Process(
        target=_stale_missing_first_open_worker,
        args=(
            str(tmp_path),
            missing_observed,
            continue_after_writer_lock,
            reread_done,
            results,
        ),
    )
    writer = context.Process(
        target=_first_open_writer_worker,
        args=(
            str(tmp_path),
            missing_observed,
            continue_after_writer_lock,
            reread_done,
            results,
        ),
    )
    reader.start()
    writer.start()
    _join_cleanly(reader)
    _join_cleanly(writer)

    messages = [results.get(timeout=5), results.get(timeout=5)]
    assert all("error" not in message[0] for message in messages)

    registry_path = tmp_path / "run_registry.json"
    raw = json.loads(registry_path.read_text(encoding="utf-8"))
    assert raw["schema_version"] == 1
    assert len(raw["runs"]) == 1
    item = next(iter(raw["runs"].values()))
    assert item["run_id"] == "writer-run"
    assert item["status"] == "completed"
    assert RunRegistry(registry_path).get(next(iter(raw["runs"]))) == item
    assert not (tmp_path / "run_registry.json.tmp").exists()
    assert list(tmp_path.glob(".run_registry.json.*.tmp")) == []


def test_two_simultaneous_clean_first_opens_are_idempotent(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    workers = [
        context.Process(
            target=_simultaneous_clean_first_open_worker,
            args=(str(tmp_path), start, results),
        )
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    start.set()
    for worker in workers:
        _join_cleanly(worker)

    messages = [results.get(timeout=5), results.get(timeout=5)]
    assert messages == [("ok", ()), ("ok", ())]
    assert json.loads((tmp_path / "run_registry.json").read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "runs": {},
    }
    assert not (tmp_path / "run_registry.json.tmp").exists()
    assert list(tmp_path.glob(".run_registry.json.*.tmp")) == []


def test_read_constructor_missing_registry_is_non_mutating(tmp_path: Path) -> None:
    workspace = tmp_path / "missing-workspace"
    registry_path = workspace / "run_registry.json"

    with pytest.raises(ValueError, match="run registry is missing"):
        RunRegistry(registry_path)

    assert not workspace.exists()
    assert not registry_path.exists()


def test_durable_workspace_history_predicate_covers_release_evidence(tmp_path: Path) -> None:
    pristine = tmp_path / "pristine"
    pristine.mkdir()
    (pristine / ".run-transactions").mkdir()
    (pristine / "decisions.jsonl").write_bytes(b"")
    assert has_durable_workspace_history(pristine) is False

    paper = tmp_path / "paper"
    paper.mkdir()
    (paper / "paper_book.json").write_text("{}\n", encoding="utf-8")
    assert has_durable_workspace_history(paper) is True

    ledger = tmp_path / "ledger"
    ledger.mkdir()
    (ledger / "decisions.jsonl").write_text("{}\n", encoding="utf-8")
    assert has_durable_workspace_history(ledger) is True

    nonregular_ledger = tmp_path / "nonregular-ledger"
    nonregular_ledger.mkdir()
    (nonregular_ledger / "decisions.jsonl").mkdir()
    assert has_durable_workspace_history(nonregular_ledger) is True

    transactions = tmp_path / "transactions"
    transactions.mkdir()
    transaction_root = transactions / ".run-transactions"
    transaction_root.mkdir()
    (transaction_root / "run-id").mkdir()
    assert has_durable_workspace_history(transactions) is True

    nonregular_transactions = tmp_path / "nonregular-transactions"
    nonregular_transactions.mkdir()
    (nonregular_transactions / ".run-transactions").write_text("unsafe", encoding="utf-8")
    assert has_durable_workspace_history(nonregular_transactions) is True

    summary = tmp_path / "summary"
    summary.mkdir()
    (summary / "run-00000000-0000-4000-8000-000000000000.json").write_text(
        "{}\n",
        encoding="utf-8",
    )
    assert has_durable_workspace_history(summary) is True


def test_missing_registry_with_surviving_history_fails_closed_without_publication(
    tmp_path: Path,
) -> None:
    (tmp_path / "paper_book.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing while durable run history exists"):
        RunRegistry.initialize_pristine(tmp_path / "run_registry.json")
    assert not (tmp_path / "run_registry.json").exists()
    assert not (tmp_path / "run_registry.json.tmp").exists()
    assert list(tmp_path.glob(".run_registry.json.*.tmp")) == []
