from __future__ import annotations

from decimal import Decimal

import autosport.betfair_timeout_reconciliation as timeout_resolution
import autosport.real_execution_ledger as ledger_module
from autosport.real_execution_ledger import ExecutionAction, ExecutionPlan, RealExecutionLedger
from autosport.supervised_provider_evidence import ProviderEvidenceError


RECORDED_AT = "2026-09-21T18:00:00+00:00"


def _durable_action() -> ExecutionAction:
    return ExecutionAction(
        action_id="action-1",
        bookmaker_id="betfair",
        account_id="durable-account",
        event_id="durable-event",
        market_id="1.111",
        selection_id="42",
        side="BACK",
        requested_odds=Decimal("2.0"),
        requested_stake=Decimal("10"),
        quote_id="durable-quote",
        quote_observed_at="2026-09-21T17:59:00+00:00",
        expires_at="2026-09-21T18:10:00+00:00",
    )


def _caller_substitution() -> ExecutionAction:
    # Keep the two fields the current timeout resolver actually checks, while
    # substituting the durable execution identity/economics that PLAN_RESERVED owns.
    return ExecutionAction(
        action_id="action-1",
        bookmaker_id="betfair",
        account_id="caller-account",
        event_id="caller-event",
        market_id="1.999",
        selection_id="99",
        side="BACK",
        requested_odds=Decimal("9.0"),
        requested_stake=Decimal("1"),
        quote_id="caller-quote",
        quote_observed_at="2026-09-21T17:59:01+00:00",
        expires_at="2026-09-21T18:09:00+00:00",
    )


def test_timeout_resolution_never_verifies_caller_substituted_action(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(ledger_module, "_now", lambda: RECORDED_AT)
    durable_action = _durable_action()
    plan = ExecutionPlan(
        plan_id="plan-1",
        bookmaker_profile_version="profile-1",
        decision_id="decision-1",
        approval_id="approval-1",
        created_at="2026-09-21T17:59:01+00:00",
        actions=(durable_action,),
    )
    ledger = RealExecutionLedger(tmp_path / "real-execution.jsonl")
    ledger.reserve_plan(plan)
    ledger.begin_attempt(
        plan_id=plan.plan_id,
        action_id=durable_action.action_id,
        attempt_id="attempt-1",
        reserved_at="2026-09-21T17:59:50+00:00",
    )
    ledger.bind_provider_order_reference(
        attempt_id="attempt-1",
        provider_id=durable_action.bookmaker_id,
    )
    ledger.mark_submitted(
        "attempt-1",
        submitted_at="2026-09-21T17:59:55+00:00",
    )
    ledger.mark_unknown(
        "attempt-1",
        reason="betfair_placeOrders_ambiguous_effect_requires_readback",
        observed_at="2026-09-21T17:59:57+00:00",
    )

    substituted = _caller_substitution()
    seen_actions: list[ExecutionAction] = []

    def stop_after_authority_boundary(actual_action, *args, **kwargs):
        seen_actions.append(actual_action)
        raise ProviderEvidenceError("stop after durable-action binding check")

    monkeypatch.setattr(
        timeout_resolution,
        "verify_betfair_provider_state",
        stop_after_authority_boundary,
    )

    try:
        timeout_resolution.resolve_betfair_timeout_provider_state(
            ledger,
            substituted,
            object(),
            attempt_id="attempt-1",
            expected_profile_sha256="a" * 64,
            readback=object(),
        )
    except (
        timeout_resolution.BetfairTimeoutResolutionError,
        ProviderEvidenceError,
    ):
        pass

    # Safe implementations may reject the mismatch before verification OR
    # re-resolve and pass the exact durable action to the canonical verifier.
    # They must never let caller-substituted action fields mint provider authority.
    assert not seen_actions or seen_actions == [durable_action]
