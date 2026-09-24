from __future__ import annotations

import hashlib
from unittest.mock import patch

import pytest

import autosport.real_execution_ledger as ledger_module
from autosport.real_execution_ledger import (
    ExecutionAction,
    ExecutionIdentityConflict,
    ExecutionPlan,
    RealExecutionLedger,
)


TS = "2026-09-21T18:00:00+00:00"
RESERVED_AT = "2026-09-21T18:00:01+00:00"
EXPIRES_AT = "2026-09-21T18:10:00+00:00"


def _action(action_id: str) -> ExecutionAction:
    return ExecutionAction(
        action_id=action_id,
        bookmaker_id="betfair",
        account_id="acct-1",
        event_id="event-1",
        market_id=f"market-{action_id}",
        selection_id=f"selection-{action_id}",
        side="BACK",
        requested_odds="2.0",
        requested_stake="10",
        quote_id=f"quote-{action_id}",
        quote_observed_at=TS,
        expires_at=EXPIRES_AT,
    )


def _plan() -> ExecutionPlan:
    return ExecutionPlan(
        plan_id="plan-1",
        bookmaker_profile_version="profile-1",
        decision_id="decision-1",
        approval_id="approval-1",
        created_at=TS,
        actions=(_action("action-a"), _action("action-b")),
    )


class _ForcedDigest:
    def __init__(self, value: str) -> None:
        self._value = value

    def hexdigest(self) -> str:
        return self._value


def test_provider_order_reference_collision_across_distinct_attempts_fails_closed_and_survives_restart(
    tmp_path,
) -> None:
    path = tmp_path / "real-execution.jsonl"
    ledger = RealExecutionLedger(path)
    ledger.reserve_plan(_plan())
    ledger.begin_attempt(
        plan_id="plan-1",
        action_id="action-a",
        attempt_id="attempt-a",
        reserved_at=RESERVED_AT,
    )
    ledger.begin_attempt(
        plan_id="plan-1",
        action_id="action-b",
        attempt_id="attempt-b",
        reserved_at=RESERVED_AT,
    )

    real_sha256 = hashlib.sha256

    def sha256_with_provider_ref_collision(data: bytes = b""):
        raw = bytes(data)
        if raw.startswith(b"betfair:acct-1:"):
            return _ForcedDigest("c" * 64)
        return real_sha256(raw)

    with patch.object(
        ledger_module.hashlib,
        "sha256",
        side_effect=sha256_with_provider_ref_collision,
    ):
        provider_ref = ledger.bind_provider_order_reference(
            attempt_id="attempt-a",
            provider_id="betfair",
        )
        assert provider_ref == "c" * 32

        # Exact same-attempt replay is idempotent.
        assert (
            ledger.bind_provider_order_reference(
                attempt_id="attempt-a",
                provider_id="betfair",
            )
            == provider_ref
        )

        # A distinct internal attempt may never acquire the same provider/account
        # instruction reference, even if the reference derivation collides.
        with pytest.raises(
            ExecutionIdentityConflict,
            match="provider order reference collision across attempts",
        ):
            ledger.bind_provider_order_reference(
                attempt_id="attempt-b",
                provider_id="betfair",
            )

        # Keep the synthetic collision model active across reopen. The sole durable
        # binding remains canonical; no server/process ordering can make attempt-b
        # become an owner after restart.
        restarted = RealExecutionLedger(path)
        restarted.verify_integrity()
        assert (
            restarted.provider_order_reference(
                attempt_id="attempt-a",
                provider_id="betfair",
            )
            == provider_ref
        )
        assert (
            restarted.provider_order_reference(
                attempt_id="attempt-b",
                provider_id="betfair",
            )
            is None
        )
