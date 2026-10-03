from __future__ import annotations

from pathlib import Path

import pytest

from autosport.real_execution_ledger import (
    AttemptState,
    ExecutionAction,
    ExecutionLedgerError,
    ExecutionPlan,
    ExecutionStateError,
    RealExecutionLedger,
    ReconciliationSnapshot,
)


TS = "2026-09-17T19:28:00+00:00"
RESERVED_AT = "2026-09-17T19:28:10+00:00"
SUBMITTED_AT = "2026-09-17T19:28:15+00:00"
UNKNOWN_AT = "2026-09-17T19:28:20+00:00"
RECONCILED_AT = "2026-09-17T19:28:30+00:00"
RETRY_RESERVED_AT = "2026-09-17T19:28:40+00:00"
EXPIRES_AT = "2026-09-17T19:29:00+00:00"


def _action() -> ExecutionAction:
    return ExecutionAction(
        action_id="a1",
        bookmaker_id="betfair",
        account_id="acct-1",
        event_id="event-1",
        market_id="market-1",
        selection_id="selection-a1",
        side="BACK",
        requested_odds="2.50",
        requested_stake="10.00",
        quote_id="quote-a1",
        quote_observed_at=TS,
        expires_at=EXPIRES_AT,
    )


def _plan() -> ExecutionPlan:
    return ExecutionPlan(
        plan_id="p1",
        bookmaker_profile_version="profile-v1",
        decision_id="decision-1",
        approval_id="approval-1",
        created_at=TS,
        actions=(_action(),),
    )


def _unknown_attempt(path: Path) -> RealExecutionLedger:
    ledger = RealExecutionLedger(path)
    ledger.reserve_plan(_plan())
    ledger.begin_attempt(
        plan_id="p1",
        action_id="a1",
        attempt_id="try-1",
        reserved_at=RESERVED_AT,
    )
    ledger.mark_submitted("try-1", submitted_at=SUBMITTED_AT)
    ledger.mark_unknown(
        "try-1",
        reason="provider_timeout",
        observed_at=UNKNOWN_AT,
    )
    assert ledger.attempt_state("try-1") is AttemptState.UNKNOWN
    assert not ledger.can_retry_action(plan_id="p1", action_id="a1")
    return ledger


def _caller_minted_not_found() -> ReconciliationSnapshot:
    # This DTO carries only caller-supplied strings/time/bool. Giving it a
    # provider-looking source label must not manufacture complete provider
    # absence authority and release an uncertain external effect to retry.
    return ReconciliationSnapshot(
        attempt_id="try-1",
        evidence_id="caller-fabricated-provider-absence",
        observed_at=RECONCILED_AT,
        external_effect_found=False,
        source="provider-readback",
    )


def _try_untrusted_not_found(
    ledger: RealExecutionLedger,
    snapshot: ReconciliationSnapshot,
) -> None:
    # A safe implementation may reject the untrusted DTO immediately. It may
    # also retain it as non-authoritative diagnostic evidence. Either behavior
    # is acceptable here; what must never follow is positive retry authority.
    try:
        ledger.reconcile_not_found(snapshot)
    except ExecutionLedgerError:
        pass


def _assert_retry_remains_blocked(ledger: RealExecutionLedger) -> None:
    assert not ledger.can_retry_action(plan_id="p1", action_id="a1")
    with pytest.raises(ExecutionStateError):
        ledger.begin_attempt(
            plan_id="p1",
            action_id="a1",
            attempt_id="try-2",
            reserved_at=RETRY_RESERVED_AT,
        )


def test_caller_minted_not_found_cannot_release_unknown_attempt_to_retry(
    tmp_path: Path,
) -> None:
    ledger = _unknown_attempt(tmp_path / "real.jsonl")

    _try_untrusted_not_found(ledger, _caller_minted_not_found())

    _assert_retry_remains_blocked(ledger)


def test_caller_minted_not_found_cannot_gain_retry_authority_after_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "real.jsonl"
    ledger = _unknown_attempt(path)

    _try_untrusted_not_found(ledger, _caller_minted_not_found())

    restarted = RealExecutionLedger(path)
    _assert_retry_remains_blocked(restarted)
