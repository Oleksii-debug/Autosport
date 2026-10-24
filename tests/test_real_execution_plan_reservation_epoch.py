from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import autosport.real_execution_ledger as ledger_module
from autosport.real_execution_ledger import (
    ExecutionAction,
    ExecutionPlan,
    RealExecutionLedger,
)


def _plan() -> ExecutionPlan:
    now = datetime.now(timezone.utc)
    action = ExecutionAction(
        action_id="action-1",
        bookmaker_id="betfair",
        account_id="acct-1",
        event_id="event-1",
        market_id="1.234",
        selection_id="42",
        side="BACK",
        requested_odds=Decimal("2.00"),
        requested_stake=Decimal("5"),
        quote_id="quote-1",
        quote_observed_at=(now - timedelta(seconds=1)).isoformat(),
        expires_at=(now + timedelta(minutes=5)).isoformat(),
    )
    return ExecutionPlan(
        plan_id="plan-1",
        bookmaker_profile_version="profiles-1",
        decision_id="decision-1",
        approval_id="approval-1",
        created_at=now.isoformat(),
        actions=(action,),
    )


def test_verified_plan_reservation_provenance_is_restart_stable(
    tmp_path: Path,
) -> None:
    path = tmp_path / "real.jsonl"
    plan = _plan()
    ledger = RealExecutionLedger(path)
    ledger.reserve_plan(plan)

    first = ledger.verified_execution_view(plan.plan_id)
    restarted = RealExecutionLedger(path).verified_execution_view(plan.plan_id)

    assert first.plan_reserved_event_id == restarted.plan_reserved_event_id
    assert first.plan_reserved_at == restarted.plan_reserved_at
    assert first.snapshot_sha256 == restarted.snapshot_sha256
    assert first.plan_fingerprint == plan.fingerprint
    assert UUID(first.plan_reserved_event_id).version == 4
    reserved_at = datetime.fromisoformat(first.plan_reserved_at)
    assert reserved_at.tzinfo is not None
    assert reserved_at.utcoffset() is not None


def test_module_clock_and_randomness_rebinding_cannot_choose_reservation_provenance(
    tmp_path: Path,
    monkeypatch,
) -> None:
    forged_time = "2000-01-01T00:00:00+00:00"
    forged_event_id = "00000000-0000-4000-8000-000000000000"
    monkeypatch.setattr(ledger_module, "_now", lambda: forged_time)
    monkeypatch.setattr(
        ledger_module.os,
        "urandom",
        lambda size: b"\\x00" * size,
    )

    ledger = RealExecutionLedger(tmp_path / "real.jsonl")
    plan = _plan()
    ledger.reserve_plan(plan)
    view = ledger.verified_execution_view(plan.plan_id)

    assert view.plan_reserved_at != forged_time
    assert view.plan_reserved_event_id != forged_event_id
    assert UUID(view.plan_reserved_event_id).version == 4


def test_in_place_now_code_mutation_cannot_choose_reservation_epoch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    forged_time = "2001-01-01T00:00:00+00:00"

    def forged_now() -> str:
        return forged_time

    monkeypatch.setattr(ledger_module._now, "__code__", forged_now.__code__)

    ledger = RealExecutionLedger(tmp_path / "real.jsonl")
    plan = _plan()
    ledger.reserve_plan(plan)
    view = ledger.verified_execution_view(plan.plan_id)

    assert view.plan_reserved_at != forged_time
