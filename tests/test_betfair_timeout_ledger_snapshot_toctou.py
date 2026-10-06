from __future__ import annotations

from decimal import Decimal

import pytest

from autosport import betfair_timeout_reconciliation as timeout_resolution
from autosport import real_execution_ledger as ledger_module
from autosport.real_execution_ledger import (
    AcknowledgementStatus,
    AttemptState,
    ExecutionAction,
    ExecutionPlan,
    ExternalAcknowledgement,
    ExternalEffectReconciliation,
    RealExecutionLedger,
)


LEDGER_TIMEOUT_BOUNDARY = "2026-09-21T18:00:00+00:00"
UNKNOWN_OBSERVED_AT = "2026-09-21T17:59:57+00:00"


def _action() -> ExecutionAction:
    return ExecutionAction(
        action_id="action-ledger-toctou",
        bookmaker_id="betfair",
        account_id="acct-1",
        event_id="event-1",
        market_id="1.234",
        selection_id="42",
        side="BACK",
        requested_odds=Decimal("2.0"),
        requested_stake=Decimal("10"),
        quote_id="quote-ledger-toctou",
        quote_observed_at="2026-09-21T17:59:00+00:00",
        expires_at="2026-09-21T18:10:00+00:00",
    )


def _ledger_with_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_module, "_now", lambda: LEDGER_TIMEOUT_BOUNDARY)
    action = _action()
    plan = ExecutionPlan(
        plan_id="plan-ledger-toctou",
        bookmaker_profile_version="profile-1",
        decision_id="decision-ledger-toctou",
        approval_id="approval-ledger-toctou",
        created_at="2026-09-21T17:59:01+00:00",
        actions=(action,),
    )
    ledger = RealExecutionLedger(tmp_path / "real-execution.jsonl")
    ledger.reserve_plan(plan)
    ledger.begin_attempt(
        plan_id=plan.plan_id,
        action_id=action.action_id,
        attempt_id="attempt-ledger-toctou",
        reserved_at="2026-09-21T17:59:50+00:00",
    )
    provider_ref = ledger.bind_provider_order_reference(
        attempt_id="attempt-ledger-toctou",
        provider_id="betfair",
    )
    ledger.mark_submitted(
        "attempt-ledger-toctou",
        submitted_at="2026-09-21T17:59:55+00:00",
    )
    ledger.mark_unknown(
        "attempt-ledger-toctou",
        reason="betfair_placeOrders_ambiguous_effect_requires_readback",
        observed_at=UNKNOWN_OBSERVED_AT,
    )
    return ledger, action, provider_ref


def _terminalize_accepted(ledger: RealExecutionLedger) -> None:
    evidence_id = "positive-race-evidence"
    external_receipt_id = "bet-race-accepted"
    ledger.reconcile_found(
        ExternalEffectReconciliation(
            attempt_id="attempt-ledger-toctou",
            evidence_id=evidence_id,
            external_receipt_id=external_receipt_id,
            observed_at="2026-09-21T18:00:01+00:00",
            source="betfair-race-test",
        )
    )
    ledger.acknowledge(
        ExternalAcknowledgement(
            attempt_id="attempt-ledger-toctou",
            external_receipt_id=external_receipt_id,
            status=AcknowledgementStatus.ACCEPTED,
            acknowledged_at="2026-09-21T18:00:02+00:00",
            accepted_odds=Decimal("2.0"),
            accepted_stake=Decimal("10"),
            reconciliation_evidence_id=evidence_id,
        )
    )


def test_timeout_authority_rejects_terminal_transition_between_ledger_reads(
    tmp_path,
    monkeypatch,
) -> None:
    ledger, action, provider_ref = _ledger_with_timeout(tmp_path, monkeypatch)
    assert provider_ref
    assert ledger.attempt_state("attempt-ledger-toctou") is AttemptState.UNKNOWN

    original_snapshot = ledger.verified_snapshot
    interposed = False

    def snapshot_after_terminal_transition():
        nonlocal interposed
        if not interposed:
            interposed = True
            monkeypatch.setattr(
                ledger_module,
                "_now",
                lambda: "2026-09-21T18:00:03+00:00",
            )
            _terminalize_accepted(ledger)
            assert (
                ledger.attempt_state("attempt-ledger-toctou")
                is AttemptState.ACCEPTED
            )
        return original_snapshot()

    monkeypatch.setattr(ledger, "verified_snapshot", snapshot_after_terminal_transition)

    with pytest.raises(
        timeout_resolution.BetfairTimeoutResolutionError,
        match="UNKNOWN|changed|snapshot|terminal|state",
    ):
        timeout_resolution._durable_timeout_authority(
            ledger,
            action,
            "attempt-ledger-toctou",
        )

    assert interposed is True
    assert ledger.attempt_state("attempt-ledger-toctou") is AttemptState.ACCEPTED
