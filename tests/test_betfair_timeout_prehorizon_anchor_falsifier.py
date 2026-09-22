from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import autosport.betfair_timeout_reconciliation as timeout_resolution
import autosport.real_execution_ledger as ledger_module
from autosport.real_execution_ledger import (
    ExecutionAction,
    ExecutionPlan,
    RealExecutionLedger,
)
from autosport.supervised_provider_evidence import VerifiedProviderAbsenceEvidence


LEDGER_TIMEOUT_BOUNDARY = "2026-09-21T18:00:00+00:00"
UNKNOWN_OBSERVED_AT = "2026-09-21T17:59:57+00:00"


def _action() -> ExecutionAction:
    return ExecutionAction(
        action_id="action-prehorizon-anchor",
        bookmaker_id="betfair",
        account_id="acct-prehorizon-anchor",
        event_id="event-prehorizon-anchor",
        market_id="1.234",
        selection_id="42",
        side="BACK",
        requested_odds=Decimal("2.0"),
        requested_stake=Decimal("10"),
        quote_id="quote-prehorizon-anchor",
        quote_observed_at="2026-09-21T17:59:00+00:00",
        expires_at="2026-09-21T18:10:00+00:00",
    )


def _ledger_with_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_module, "_now", lambda: LEDGER_TIMEOUT_BOUNDARY)
    action = _action()
    plan = ExecutionPlan(
        plan_id="plan-prehorizon-anchor",
        bookmaker_profile_version="profile-prehorizon-anchor",
        decision_id="decision-prehorizon-anchor",
        approval_id="approval-prehorizon-anchor",
        created_at="2026-09-21T17:59:01+00:00",
        actions=(action,),
    )
    ledger = RealExecutionLedger(tmp_path / "real-execution.jsonl")
    ledger.reserve_plan(plan)
    ledger.begin_attempt(
        plan_id=plan.plan_id,
        action_id=action.action_id,
        attempt_id="attempt-prehorizon-anchor",
        reserved_at="2026-09-21T17:59:50+00:00",
    )
    provider_ref = ledger.bind_provider_order_reference(
        attempt_id="attempt-prehorizon-anchor",
        provider_id="betfair",
    )
    ledger.mark_submitted(
        "attempt-prehorizon-anchor",
        submitted_at="2026-09-21T17:59:55+00:00",
    )
    ledger.mark_unknown(
        "attempt-prehorizon-anchor",
        reason="betfair_placeOrders_ambiguous_effect_requires_readback",
        observed_at=UNKNOWN_OBSERVED_AT,
    )
    return ledger, action, provider_ref


def _absence(
    *,
    observed_at: str,
    provider_ref: str,
) -> VerifiedProviderAbsenceEvidence:
    return VerifiedProviderAbsenceEvidence(
        bookmaker_id="betfair",
        account_id="acct-prehorizon-anchor",
        action_id="action-prehorizon-anchor",
        adapter_id="betfair-exchange-jsonrpc-readonly",
        adapter_version="1",
        profile_version=1,
        event_id="event-prehorizon-anchor",
        market_id="1.234",
        selection_id="42",
        observed_at=observed_at,
        current_source_payload_sha256="1" * 64,
        cleared_source_payload_sha256="2" * 64,
        evidence_id="3" * 64,
        provider_order_ref=provider_ref,
    )


def test_prehorizon_negative_capture_cannot_preage_monotonic_anchor(
    tmp_path,
    monkeypatch,
) -> None:
    ledger, action, provider_ref = _ledger_with_timeout(tmp_path, monkeypatch)
    current = {
        "wall": "2026-09-21T18:00:01+00:00",
        "mono": 1_000_000_000,
    }

    def fake_verify(*_args, **_kwargs):
        return _absence(
            observed_at=current["wall"],
            provider_ref=provider_ref,
        )

    monkeypatch.setattr(
        timeout_resolution,
        "verify_betfair_provider_state",
        fake_verify,
    )
    monkeypatch.setattr(
        timeout_resolution,
        "_betfair_readback_capture_started_at",
        lambda _readback: current["wall"],
    )
    monkeypatch.setattr(
        timeout_resolution,
        "_betfair_readback_capture_started_monotonic_ns",
        lambda _readback: current["mono"],
    )
    monkeypatch.setattr(
        timeout_resolution,
        "_absence_capture_floor",
        lambda _readback: current["wall"],
    )
    monkeypatch.setattr(
        timeout_resolution,
        "_absence_capture_ceiling",
        lambda _readback: current["wall"],
    )

    first = timeout_resolution._resolve_betfair_timeout_provider_state_core(
        ledger,
        action,
        object(),
        attempt_id="attempt-prehorizon-anchor",
        expected_profile_sha256="a" * 64,
        readback=object(),
    )
    assert (
        first.kind
        is timeout_resolution.BetfairTimeoutResolutionKind.INDETERMINATE_BEFORE_VISIBILITY_HORIZON
    )
    assert first.evidence is None

    # This is the first capture that is actually eligible with respect to the
    # provider's UTC visibility horizon.  A pre-horizon read must not have aged the
    # process-local monotonic fence, so this capture must establish the anchor rather
    # than immediately mint retry-authoritative absence.
    current["wall"] = "2026-09-21T18:00:16+00:00"
    current["mono"] = 16_000_000_000
    second = timeout_resolution._resolve_betfair_timeout_provider_state_core(
        ledger,
        action,
        object(),
        attempt_id="attempt-prehorizon-anchor",
        expected_profile_sha256="a" * 64,
        readback=object(),
    )
    assert (
        second.kind
        is timeout_resolution.BetfairTimeoutResolutionKind.INDETERMINATE_BEFORE_VISIBILITY_HORIZON
    )
    assert second.evidence is None

    # Only a further eligible capture after one full monotonic provider horizon may
    # become definitive.
    current["wall"] = "2026-09-21T18:00:31+00:00"
    current["mono"] = 31_000_000_000
    third = timeout_resolution._resolve_betfair_timeout_provider_state_core(
        ledger,
        action,
        object(),
        attempt_id="attempt-prehorizon-anchor",
        expected_profile_sha256="a" * 64,
        readback=object(),
    )
    assert (
        third.kind
        is timeout_resolution.BetfairTimeoutResolutionKind.ABSENT_AFTER_VISIBILITY_HORIZON
    )
    assert isinstance(third.evidence, VerifiedProviderAbsenceEvidence)
