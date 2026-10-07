from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import multiprocessing
from pathlib import Path
from queue import Empty

import pytest

import autosport.policy_utility_terminalizer as terminalizer_module
from autosport.policy_utility_evidence import (
    AuthorityRef,
    DecisionKind,
    PolicyUtilityEvidence,
    UtilityCompleteness,
    UtilityTruthClass,
)
from autosport.policy_utility_terminalizer import PolicyUtilityTerminalizer


_SHA_A = "a" * 64
_SHA_B = "b" * 64
_SHA_C = "c" * 64
_SHA_D = "d" * 64
_SHA_E = "e" * 64


def _evidence(
    *,
    episode_id: str = "episode-1",
    utility_definition_version: str = "v1",
    utility_definition_sha256: str = _SHA_E,
) -> PolicyUtilityEvidence:
    return PolicyUtilityEvidence(
        environment_id="env-1",
        episode_id=episode_id,
        action_id="action-1",
        outcome_id="outcome-1",
        reward_id="reward-1",
        transition_id="transition-1",
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
        utility_definition_version=utility_definition_version,
        utility_definition_sha256=utility_definition_sha256,
        completeness=UtilityCompleteness.INCOMPLETE,
        truth_class=UtilityTruthClass.OBSERVED,
        decision_kind=DecisionKind.POSITIONED,
        available_at=datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc),
        currency="EUR",
        utility_value=Decimal("0.20"),
        authority_refs=(AuthorityRef("campaign-economics", "econ-1", _SHA_A),),
    )


def _terminalize_worker(
    path: str,
    utility_definition_version: str,
    utility_definition_sha256: str,
    start,
    results,
) -> None:
    evidence = _evidence(
        utility_definition_version=utility_definition_version,
        utility_definition_sha256=utility_definition_sha256,
    )
    start.wait()
    try:
        receipt = PolicyUtilityTerminalizer.from_path(path).terminalize(evidence)
    except BaseException as exc:  # child-process evidence must reach the parent
        results.put(("error", type(exc).__name__, str(exc)))
    else:
        results.put(("ok", receipt.persisted, receipt.evidence_id, receipt.record_id))


def _run_two_processes(
    path: Path,
    left: tuple[str, str],
    right: tuple[str, str],
) -> list[tuple[object, ...]]:
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_terminalize_worker,
            args=(str(path), version, digest, start, results),
        )
        for version, digest in (left, right)
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
            raise AssertionError("child process produced no terminalization result") from exc
    return observed


def test_two_process_conflicting_same_semantic_key_preserves_both_non_authoritative_records(
    tmp_path,
) -> None:
    path = tmp_path / "utility.jsonl"

    observed = _run_two_processes(
        path,
        ("v1", _SHA_E),
        ("v2", _SHA_B),
    )

    assert all(item[0] == "ok" for item in observed)
    assert sorted(item[1] for item in observed) == [True, True]
    assert len({item[2] for item in observed}) == 2
    assert len({item[3] for item in observed}) == 2

    terminalizer = PolicyUtilityTerminalizer.from_path(path)
    assert len(terminalizer.records()) == 2
    assert not path.exists()
    assert terminalizer.journal_path.exists()


def test_two_process_identical_retry_converges_to_one_terminal_record(tmp_path) -> None:
    path = tmp_path / "utility.jsonl"

    observed = _run_two_processes(
        path,
        ("v1", _SHA_E),
        ("v1", _SHA_E),
    )

    assert all(item[0] == "ok" for item in observed)
    assert sorted(item[1] for item in observed) == [False, True]
    assert len({item[2] for item in observed}) == 1
    assert len({item[3] for item in observed}) == 1
    terminalizer = PolicyUtilityTerminalizer.from_path(path)
    assert len(terminalizer.records()) == 1
    assert not path.exists()


def test_interrupted_publish_preserves_previous_terminal_image_and_retry_converges(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "utility.jsonl"
    terminalizer = PolicyUtilityTerminalizer.from_path(path)
    first = _evidence()
    second = _evidence(episode_id="episode-2")

    assert terminalizer.terminalize(first).persisted is True
    journal = terminalizer.journal_path
    before = journal.read_bytes()

    real_replace = terminalizer_module._durable_replace
    with monkeypatch.context() as patch:
        def fail_replace(source, destination):
            raise OSError("injected durable publication failure")

        patch.setattr(terminalizer_module, "_durable_replace", fail_replace)
        with pytest.raises(OSError, match="injected durable publication failure"):
            terminalizer.terminalize(second)

    assert terminalizer_module._durable_replace is real_replace
    assert journal.read_bytes() == before
    assert len(terminalizer.records()) == 1
    assert terminalizer.records()[0].candidate_evidence_id == first.evidence_id
    assert not list(tmp_path.glob(f".{journal.name}.*.tmp"))
    assert not path.exists()

    retry = PolicyUtilityTerminalizer.from_path(path).terminalize(second)
    assert retry.persisted is True
    exact_retry = PolicyUtilityTerminalizer.from_path(path).terminalize(second)
    assert exact_retry.persisted is False
    records = PolicyUtilityTerminalizer.from_path(path).records()
    assert tuple(record.candidate_evidence_id for record in records) == (
        first.evidence_id,
        second.evidence_id,
    )
