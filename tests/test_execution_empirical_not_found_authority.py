from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from autosport.execution_empirical_evidence import (
    EmpiricalExecutionEvidenceError,
    build_empirical_execution_evidence,
)
from autosport.real_execution_ledger import (
    AttemptState,
    ExecutionAction,
    ExecutionLedgerError,
    ExecutionPlan,
    RealExecutionLedger,
    ReconciliationSnapshot,
)


TS = "2026-09-17T19:28:00+00:00"
RESERVED_AT = "2026-09-17T19:28:10+00:00"
SUBMITTED_AT = "2026-09-17T19:28:15+00:00"
UNKNOWN_AT = "2026-09-17T19:28:20+00:00"
RECONCILED_AT = "2026-09-17T19:28:30+00:00"
EXPIRES_AT = "2026-09-17T19:29:00+00:00"


def _plan() -> ExecutionPlan:
    action = ExecutionAction(
        action_id="a1",
        bookmaker_id="betfair",
        account_id="acct-1",
        event_id="event-1",
        market_id="market-1",
        selection_id="selection-a1",
        side="BACK",
        requested_odds=Decimal("2.50"),
        requested_stake=Decimal("10.00"),
        quote_id="quote-a1",
        quote_observed_at=TS,
        expires_at=EXPIRES_AT,
    )
    return ExecutionPlan(
        plan_id="p1",
        bookmaker_profile_version="profile-v1",
        decision_id="decision-1",
        approval_id="approval-1",
        created_at=TS,
        actions=(action,),
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
    return ledger


def _inject_caller_minted_provider_not_found(ledger: RealExecutionLedger) -> None:
    snapshot = ReconciliationSnapshot(
        attempt_id="try-1",
        evidence_id="caller-fabricated-provider-absence",
        observed_at=RECONCILED_AT,
        external_effect_found=False,
        source="provider-readback",
    )
    try:
        ledger.reconcile_not_found(snapshot)
    except ExecutionLedgerError:
        # A safe upstream #1107/#794 repair may reject this assertion before it
        # reaches durable state. The empirical projection must remain safe either way.
        pass


def _assert_untrusted_not_found_is_not_laundered(
    ledger: RealExecutionLedger,
) -> None:
    try:
        evidence = build_empirical_execution_evidence(
            ledger,
            attempt_id="try-1",
        )
    except EmpiricalExecutionEvidenceError:
        # Rejecting an unverified provider-absence record is fail-closed.
        return

    assert not (
        evidence.attempt_state == AttemptState.RECONCILED_NOT_FOUND.value
        and evidence.ledger_terminal
    ), (
        "caller-minted provider NOT_FOUND must not become terminal empirical "
        "no-effect truth merely because the unsafe ledger event was durable"
    )


def test_caller_minted_not_found_is_not_laundered_into_empirical_terminal_truth(
    tmp_path: Path,
) -> None:
    ledger = _unknown_attempt(tmp_path / "real.jsonl")
    _inject_caller_minted_provider_not_found(ledger)

    _assert_untrusted_not_found_is_not_laundered(ledger)


def test_restart_does_not_launder_caller_minted_not_found_into_empirical_truth(
    tmp_path: Path,
) -> None:
    path = tmp_path / "real.jsonl"
    ledger = _unknown_attempt(path)
    _inject_caller_minted_provider_not_found(ledger)

    restarted = RealExecutionLedger(path)
    _assert_untrusted_not_found_is_not_laundered(restarted)
