from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import multiprocessing
from pathlib import Path
from queue import Empty

import pytest

from autosport.policy_utility_evidence import (
    AuthorityRef,
    DecisionKind,
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
    tmp_path,
) -> None:
    path = tmp_path / "utility.jsonl"
    evidence = _evidence(episode_id="episode-dir-sync")
    seen_stages: list[str] = []

    def fail_before_directory_sync(stage: str) -> None:
        seen_stages.append(stage)
        if stage == "after_replace_before_directory_fsync":
            raise RuntimeError("injected directory-sync boundary failure")

    store = PolicyUtilityStore(path, fault_hook=fail_before_directory_sync)
    with pytest.raises(RuntimeError, match="directory-sync boundary failure"):
        store.append(evidence)

    assert "before_replace" in seen_stages
    assert "after_replace_before_directory_fsync" in seen_stages
    assert "after_directory_fsync" not in seen_stages

    # Publication may already be visible after replace, but the failed caller
    # never received True. A clean restart converges idempotently on exactly one
    # complete record rather than creating a second commit or a partial line.
    reopened = PolicyUtilityStore(path)
    assert reopened.list() == (evidence,)
    assert reopened.append(evidence) is False
    assert reopened.list() == (evidence,)
