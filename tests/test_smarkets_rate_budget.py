from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import threading

import pytest

from autosport.smarkets_rate_budget import (
    SmarketsAccountRateBudget,
    SmarketsRateBudgetError,
    SmarketsRateBudgetObservation,
)


T0 = datetime(2026, 9, 22, 20, 0, tzinfo=timezone.utc)


def obs(
    *,
    account: str = "acct-1",
    limit: int = 1200,
    remaining: int = 1199,
    observed_at: datetime = T0,
    reset_at: datetime = T0 + timedelta(seconds=60),
    status: int = 200,
    error_type: str | None = None,
) -> SmarketsRateBudgetObservation:
    return SmarketsRateBudgetObservation(
        account_id=account,
        limit=limit,
        remaining=remaining,
        window_seconds=60,
        observed_at=observed_at,
        reset_at=reset_at,
        http_status=status,
        error_type=error_type,
    )


def store(path: Path, *, approved_limit: int = 1200) -> SmarketsAccountRateBudget:
    return SmarketsAccountRateBudget(
        path,
        account_id="acct-1",
        approved_limit=approved_limit,
        approved_window_seconds=60,
    )


def test_two_session_generations_share_one_remaining_slot(tmp_path: Path) -> None:
    path = tmp_path / "budget.sqlite3"
    first = store(path)
    second = store(path)
    first.record_observation(obs(limit=1200, remaining=1))

    a = first.reserve_request(account_id="acct-1", session_generation="login-A", now=T0)
    b = second.reserve_request(account_id="acct-1", session_generation="login-B", now=T0)

    assert a.request_budget_available is True
    assert a.effective_remaining_before == 1
    assert a.effective_remaining_after == 0
    assert b.request_budget_available is False
    assert b.reason == "budget_exhausted"


def test_restart_does_not_replenish_same_observation(tmp_path: Path) -> None:
    path = tmp_path / "budget.sqlite3"
    original = store(path)
    evidence = obs(limit=10, remaining=2)
    original.record_observation(evidence)
    assert original.reserve_request(account_id="acct-1", session_generation="one", now=T0).request_budget_available

    restarted = store(path)
    restarted.record_observation(evidence)  # replay is idempotent, not a reset
    assert restarted.reserve_request(account_id="acct-1", session_generation="two", now=T0).request_budget_available
    denied = restarted.reserve_request(account_id="acct-1", session_generation="three", now=T0)
    assert not denied.request_budget_available
    assert denied.reason == "budget_exhausted"


def test_provider_429_waits_without_consuming(tmp_path: Path) -> None:
    budget = store(tmp_path / "budget.sqlite3")
    budget.record_observation(obs(remaining=17, status=429, error_type="RATE_LIMIT_EXCEEDED"))
    decision = budget.reserve_request(account_id="acct-1", session_generation="s1", now=T0)
    assert not decision.request_budget_available
    assert decision.reason == "provider_rate_limited"
    assert decision.effective_remaining_before == decision.effective_remaining_after


def test_malformed_429_type_fails_closed_at_evidence_boundary() -> None:
    with pytest.raises(SmarketsRateBudgetError, match="RATE_LIMIT_EXCEEDED"):
        obs(remaining=0, status=429, error_type="TOO_MANY_REQUESTS")


def test_zero_remaining_waits_even_after_2xx(tmp_path: Path) -> None:
    budget = store(tmp_path / "budget.sqlite3")
    budget.record_observation(obs(remaining=0))
    decision = budget.reserve_request(account_id="acct-1", session_generation="s", now=T0)
    assert not decision.request_budget_available
    assert decision.reason == "budget_exhausted"


def test_5xx_with_apparent_remaining_does_not_trigger_retry_burst(tmp_path: Path) -> None:
    budget = store(tmp_path / "budget.sqlite3")
    budget.record_observation(obs(remaining=1100, status=503))
    decision = budget.reserve_request(account_id="acct-1", session_generation="s", now=T0)
    assert not decision.request_budget_available
    assert decision.reason == "transport_ambiguous_wait"


def test_wrong_account_cannot_consume_or_fork_quota(tmp_path: Path) -> None:
    budget = store(tmp_path / "budget.sqlite3")
    with pytest.raises(SmarketsRateBudgetError, match="account mismatch"):
        budget.record_observation(obs(account="acct-2"))
    budget.record_observation(obs(remaining=3))
    with pytest.raises(SmarketsRateBudgetError, match="account mismatch"):
        budget.reserve_request(account_id="acct-2", session_generation="s", now=T0)


@pytest.mark.parametrize(
    ("limit", "remaining"),
    [(0, 0), (10, -1), (10, 11)],
)
def test_invalid_limit_or_remaining_fails_closed(limit: int, remaining: int) -> None:
    with pytest.raises(SmarketsRateBudgetError):
        obs(limit=limit, remaining=remaining)


def test_negative_or_past_reset_fails_closed() -> None:
    with pytest.raises(SmarketsRateBudgetError, match="reset_at"):
        obs(reset_at=T0 - timedelta(microseconds=1))


def test_approved_limit_is_stricter_than_provider_default(tmp_path: Path) -> None:
    budget = store(tmp_path / "budget.sqlite3", approved_limit=600)
    # Provider says 300 of 1200 account requests already used.  The approved
    # 600/request window therefore has only 300 requests left, not 600/900.
    budget.record_observation(obs(limit=1200, remaining=900))
    first = budget.reserve_request(account_id="acct-1", session_generation="s", now=T0)
    assert first.request_budget_available
    assert first.effective_remaining_before == 300
    assert first.effective_remaining_after == 299


def test_unknown_or_mismatched_approval_window_fails_closed(tmp_path: Path) -> None:
    budget = SmarketsAccountRateBudget(
        tmp_path / "budget.sqlite3",
        account_id="acct-1",
        approved_limit=600,
        approved_window_seconds=30,
    )
    with pytest.raises(SmarketsRateBudgetError, match="windows must match"):
        budget.record_observation(obs())


def test_same_evidence_replay_does_not_restore_consumed_reservation(tmp_path: Path) -> None:
    budget = store(tmp_path / "budget.sqlite3")
    evidence = obs(limit=5, remaining=1)
    budget.record_observation(evidence)
    assert budget.reserve_request(account_id="acct-1", session_generation="s1", now=T0).request_budget_available
    budget.record_observation(evidence)
    denied = budget.reserve_request(account_id="acct-1", session_generation="s2", now=T0)
    assert not denied.request_budget_available


def test_conflicting_same_timestamp_observation_is_rejected(tmp_path: Path) -> None:
    budget = store(tmp_path / "budget.sqlite3")
    budget.record_observation(obs(remaining=10))
    with pytest.raises(SmarketsRateBudgetError, match="conflicting"):
        budget.record_observation(obs(remaining=9))


def test_remaining_cannot_increase_inside_same_reset_window(tmp_path: Path) -> None:
    budget = store(tmp_path / "budget.sqlite3")
    reset = T0 + timedelta(seconds=60)
    budget.record_observation(obs(remaining=900, reset_at=reset))
    with pytest.raises(SmarketsRateBudgetError, match="increased"):
        budget.record_observation(
            obs(
                remaining=901,
                observed_at=T0 + timedelta(seconds=1),
                reset_at=reset,
            )
        )


def test_new_reset_window_can_replenish_only_with_fresh_observation(tmp_path: Path) -> None:
    budget = store(tmp_path / "budget.sqlite3")
    budget.record_observation(obs(limit=2, remaining=1, reset_at=T0 + timedelta(seconds=1)))
    assert budget.reserve_request(account_id="acct-1", session_generation="old", now=T0).request_budget_available
    stale = budget.reserve_request(
        account_id="acct-1",
        session_generation="new-session",
        now=T0 + timedelta(seconds=1),
    )
    assert not stale.request_budget_available
    assert stale.reason == "reset_refresh_required"

    budget.record_observation(
        obs(
            limit=2,
            remaining=2,
            observed_at=T0 + timedelta(seconds=1),
            reset_at=T0 + timedelta(seconds=61),
        )
    )
    assert budget.reserve_request(
        account_id="acct-1",
        session_generation="new-session",
        now=T0 + timedelta(seconds=1),
    ).request_budget_available


def test_parallel_store_instances_never_double_spend_one_slot(tmp_path: Path) -> None:
    path = tmp_path / "budget.sqlite3"
    seed = store(path)
    seed.record_observation(obs(limit=10, remaining=1))
    decisions = []
    errors = []

    def worker(name: str) -> None:
        try:
            decisions.append(
                store(path).reserve_request(
                    account_id="acct-1", session_generation=name, now=T0
                )
            )
        except SmarketsRateBudgetError as exc:
            # SQLite lock contention is a fail-closed outcome, not permission.
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(f"s{i}",)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(d.request_budget_available for d in decisions) == 1
    assert all(not d.request_budget_available for d in decisions if not d.request_budget_available)
    assert len(decisions) + len(errors) == 8


def test_decision_never_mints_higher_authority(tmp_path: Path) -> None:
    budget = store(tmp_path / "budget.sqlite3")
    budget.record_observation(obs(remaining=2))
    decision = budget.reserve_request(account_id="acct-1", session_generation="s", now=T0)
    assert decision.request_budget_available
    assert decision.provider_origin_proven is False
    assert decision.execution_authorized is False
    assert decision.real_money_execution is False


def test_unobserved_budget_fails_closed(tmp_path: Path) -> None:
    decision = store(tmp_path / "budget.sqlite3").reserve_request(
        account_id="acct-1", session_generation="s", now=T0
    )
    assert not decision.request_budget_available
    assert decision.reason == "budget_unobserved"
