from __future__ import annotations

import multiprocessing
from dataclasses import replace
from decimal import Decimal

import pytest

from autosport.economic_goal import EconomicGoalContract, EconomicGoalContractError
from autosport.economic_goal_store import (
    EconomicGoalStore,
    economic_goal_from_payload,
    economic_goal_to_payload,
)
from autosport.workspace_lock import (
    WorkspaceEconomicLock,
    WorkspaceEconomicLockBusyError,
)


def _goal() -> EconomicGoalContract:
    return EconomicGoalContract(
        goal_id="owner-goal-v1",
        revision=1,
        bankroll_id="paper-main",
        currency="EUR",
    )


def _hold_workspace_lock(workspace: str, ready, release) -> None:
    with WorkspaceEconomicLock(workspace):
        ready.set()
        if not release.wait(20):
            raise RuntimeError("test lock holder timed out waiting for release")


def test_automatic_writer_cannot_validate_or_publish_behind_active_economic_writer(
    tmp_path,
) -> None:
    previous = _goal()
    candidate = replace(previous, revision=2, max_stake_fraction=Decimal("0.01"))
    store = EconomicGoalStore(tmp_path)
    store.initialize_owner(previous)
    before = store.path.read_bytes()

    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    process = context.Process(
        target=_hold_workspace_lock,
        args=(str(tmp_path), ready, release),
    )
    process.start()
    assert ready.wait(20), "child process did not acquire workspace economic lock"
    try:
        with pytest.raises(WorkspaceEconomicLockBusyError):
            store.persist_automatic_successor(candidate)
        assert store.path.read_bytes() == before
        assert store.load() == previous
    finally:
        release.set()
        process.join(20)
        if process.is_alive():
            process.terminate()
            process.join(10)
            pytest.fail("child lock holder did not exit")
        assert process.exitcode == 0


def test_noncanonical_decimal_text_is_rejected_in_persisted_authority() -> None:
    payload = economic_goal_to_payload(_goal())
    body = payload["contract"]
    assert isinstance(body, dict)
    body["max_stake_fraction"] = "+0.02"

    with pytest.raises(EconomicGoalContractError, match="canonical Decimal text"):
        economic_goal_from_payload(payload)
