from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import multiprocessing
import os
from pathlib import Path
from queue import Empty
import sys
from types import SimpleNamespace

import pytest

import autosport.policy_utility_evidence as policy_utility_module
from autosport.policy_utility_evidence import (
    AuthorityRef,
    DecisionKind,
    PolicyUtilityError,
    PolicyUtilityEvidence,
    PolicyUtilityStore,
    UtilityCompleteness,
    UtilityTruthClass,
)


_SHA_A = "a" * 64
_SHA_B = "b" * 64
_SHA_C = "c" * 64
_SHA_D = "d" * 64
_SHA_E = "e" * 64


def _evidence(*, episode_id: str) -> PolicyUtilityEvidence:
    return PolicyUtilityEvidence(
        environment_id="env-1",
        episode_id=episode_id,
        action_id=f"action-{episode_id}",
        outcome_id=f"outcome-{episode_id}",
        reward_id=f"reward-{episode_id}",
        transition_id=f"transition-{episode_id}",
        policy_id="policy-1",
        model_id="model-1",
        strategy_id="strategy-1",
        config_sha256=_SHA_A,
        protocol_sha256=_SHA_B,
        economic_goal_fingerprint=_SHA_C,
        risk_fingerprint=_SHA_D,
        bankroll_id="bankroll-1",
        portfolio_identity="paper-portfolio",
        utility_definition_family="owner-net-utility",
        utility_definition_version="v1",
        utility_definition_sha256=_SHA_E,
        completeness=UtilityCompleteness.INCOMPLETE,
        truth_class=UtilityTruthClass.OBSERVED,
        decision_kind=DecisionKind.POSITIONED,
        available_at=datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc),
        currency="EUR",
        utility_value=Decimal("0.20"),
        authority_refs=(AuthorityRef("campaign-economics", "econ-1", _SHA_A),),
    )


def _append_worker(path: str, episode_id: str, start, results) -> None:
    evidence = _evidence(episode_id=episode_id)
    store = PolicyUtilityStore(path)
    start.wait()
    try:
        appended = store.append(evidence)
    except BaseException as exc:  # child-process evidence must reach parent
        results.put(("error", type(exc).__name__, str(exc)))
    else:
        results.put(("ok", appended, evidence.evidence_id))


def test_canonical_store_cross_process_writers_preserve_both_commits(tmp_path) -> None:
    path = tmp_path / "utility.jsonl"
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_append_worker,
            args=(str(path), episode_id, start, results),
        )
        for episode_id in ("episode-a", "episode-b")
    ]

    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(20)
        assert process.exitcode == 0

    observed: list[tuple[object, ...]] = []
    for _ in processes:
        try:
            observed.append(results.get(timeout=5))
        except Empty as exc:  # pragma: no cover - diagnostic guard
            raise AssertionError("child process produced no append result") from exc

    assert all(item[0] == "ok" for item in observed)
    assert sorted(item[1] for item in observed) == [True, True]
    durable = PolicyUtilityStore(path).list()
    assert len(durable) == 2
    assert {item.evidence_id for item in durable} == {item[2] for item in observed}


def test_directory_sync_boundary_failure_returns_no_receipt_and_retry_converges(
    monkeypatch,
    tmp_path,
) -> None:
    if os.name == "nt":
        pytest.skip("POSIX directory-fsync boundary")

    path = tmp_path / "utility.jsonl"
    evidence = _evidence(episode_id="episode-dir-sync")

    def fail_directory_sync(_path: Path) -> None:
        raise OSError("injected directory-sync boundary failure")

    monkeypatch.setattr(policy_utility_module, "_fsync_directory", fail_directory_sync)
    store = PolicyUtilityStore(path)
    with pytest.raises(PolicyUtilityError, match="unable to durably publish"):
        store.append(evidence)

    # POSIX replace may already be visible when the directory fsync fails, but
    # the failed caller receives no True receipt. A clean restart converges
    # idempotently on exactly one complete record rather than duplicating it.
    monkeypatch.undo()
    reopened = PolicyUtilityStore(path)
    assert reopened.list() == (evidence,)
    assert reopened.append(evidence) is False
    assert reopened.list() == (evidence,)


def test_store_does_not_expose_reentrant_fault_callback(tmp_path) -> None:
    path = tmp_path / "utility.jsonl"
    with pytest.raises(TypeError):
        PolicyUtilityStore(path, fault_hook=lambda _stage: None)  # type: ignore[call-arg]


def test_windows_write_through_replace_uses_atomic_write_through_flags(monkeypatch, tmp_path) -> None:
    source = tmp_path / "source.tmp"
    destination = tmp_path / "utility.jsonl"
    source.write_bytes(b"successor")
    calls: list[tuple[str, str, int]] = []

    class FakeMoveFileEx:
        argtypes = None
        restype = None

        def __call__(self, source_path, destination_path, flags):
            calls.append((source_path, destination_path, flags))
            return 1

    fake_move = FakeMoveFileEx()
    fake_ctypes = SimpleNamespace(
        windll=SimpleNamespace(kernel32=SimpleNamespace(MoveFileExW=fake_move)),
        c_wchar_p=object(),
        c_uint=object(),
        c_int=object(),
        WinError=lambda: OSError("unexpected Windows replacement failure"),
    )
    monkeypatch.setitem(sys.modules, "ctypes", fake_ctypes)

    policy_utility_module._replace_windows_write_through(source, destination)

    assert calls == [(str(source), str(destination), 0x1 | 0x8)]


def test_windows_write_through_failure_cannot_report_durable_success(monkeypatch, tmp_path) -> None:
    source = tmp_path / "source.tmp"
    destination = tmp_path / "utility.jsonl"
    source.write_bytes(b"successor")

    class FakeMoveFileEx:
        argtypes = None
        restype = None

        def __call__(self, source_path, destination_path, flags):
            return 0

    fake_move = FakeMoveFileEx()
    fake_ctypes = SimpleNamespace(
        windll=SimpleNamespace(kernel32=SimpleNamespace(MoveFileExW=fake_move)),
        c_wchar_p=object(),
        c_uint=object(),
        c_int=object(),
        WinError=lambda: OSError("injected Windows write-through failure"),
    )
    monkeypatch.setitem(sys.modules, "ctypes", fake_ctypes)

    with pytest.raises(OSError, match="write-through failure"):
        policy_utility_module._replace_windows_write_through(source, destination)

    assert source.read_bytes() == b"successor"
    assert not destination.exists()


def test_store_wraps_durable_replace_failure_and_returns_no_receipt(monkeypatch, tmp_path) -> None:
    path = tmp_path / "utility.jsonl"
    evidence = _evidence(episode_id="episode-write-through-failure")

    def fail_durable_replace(source, destination):
        raise OSError("injected platform durable replace failure")

    monkeypatch.setattr(policy_utility_module, "_durable_replace", fail_durable_replace)
    store = PolicyUtilityStore(path)

    with pytest.raises(PolicyUtilityError, match="unable to durably publish"):
        store.append(evidence)

    assert not path.exists()
    assert not list(tmp_path.glob(f".{path.name}.*.tmp"))
