from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from autosport.smarkets_rate_budget import (
    SmarketsAccountRateBudget,
    SmarketsRateBudgetError,
    SmarketsRateBudgetObservation,
)


T0 = datetime(2026, 9, 22, 20, 0, tzinfo=timezone.utc)


def test_reservation_cannot_be_completed_by_replaying_issuing_observation(tmp_path: Path) -> None:
    budget = SmarketsAccountRateBudget(
        tmp_path / "budget.sqlite3",
        account_id="acct-1",
        approved_limit=5,
        approved_window_seconds=60,
    )
    evidence = SmarketsRateBudgetObservation(
        account_id="acct-1",
        limit=5,
        remaining=2,
        window_seconds=60,
        observed_at=T0,
        reset_at=T0 + timedelta(seconds=60),
        http_status=200,
    )
    budget.record_observation(evidence)
    reserved = budget.reserve_request(
        account_id="acct-1", session_generation="s1", now=T0
    )
    assert reserved.reservation_id is not None

    with pytest.raises(SmarketsRateBudgetError, match="fresh provider observation"):
        budget.record_observation(evidence, reservation_id=reserved.reservation_id)

    # The rejected replay must not release the pending slot.
    second = budget.reserve_request(
        account_id="acct-1", session_generation="s2", now=T0
    )
    assert second.request_budget_available
    assert second.effective_remaining_before == 1
    exhausted = budget.reserve_request(
        account_id="acct-1", session_generation="s3", now=T0
    )
    assert not exhausted.request_budget_available
    assert exhausted.reason == "budget_exhausted"