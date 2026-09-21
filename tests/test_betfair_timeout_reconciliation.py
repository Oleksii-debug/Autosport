from __future__ import annotations

from decimal import Decimal

import pytest

import autosport.betfair_timeout_reconciliation as timeout_resolution
import autosport.real_execution_ledger as ledger_module
from autosport.real_execution_ledger import (
    AcknowledgementStatus,
    ExecutionAction,
    ExecutionPlan,
    RealExecutionLedger,
)
from autosport.supervised_provider_evidence import (
    VerifiedProviderAbsenceEvidence,
    VerifiedProviderEffectEvidence,
)


LEDGER_TIMEOUT_BOUNDARY = "2026-09-21T18:00:00+00:00"
UNKNOWN_OBSERVED_AT = "2026-09-21T17:59:57+00:00"


def _action() -> ExecutionAction:
    return ExecutionAction(
        action_id="action-1",
        bookmaker_id="betfair",
        account_id="acct-1",
        event_id="event-1",
        market_id="1.234",
        selection_id="42",
        side="BACK",
        requested_odds=Decimal("2.0"),
        requested_stake=Decimal("10"),
        quote_id="quote-1",
        quote_observed_at="2026-09-21T17:59:00+00:00",
        expires_at="2026-09-21T18:10:00+00:00",
    )


def _ledger_with_timeout(
    tmp_path,
    monkeypatch,
    *,
    reason: str = "betfair_placeOrders_ambiguous_effect_requires_readback",
    bind_provider_ref: bool = True,
):
    monkeypatch.setattr(ledger_module, "_now", lambda: LEDGER_TIMEOUT_BOUNDARY)
    action = _action()
    plan = ExecutionPlan(
        plan_id="plan-1",
        bookmaker_profile_version="profile-1",
        decision_id="decision-1",
        approval_id="approval-1",
        created_at="2026-09-21T17:59:01+00:00",
        actions=(action,),
    )
    path = tmp_path / "real-execution.jsonl"
    ledger = RealExecutionLedger(path)
    ledger.reserve_plan(plan)
    ledger.begin_attempt(
        plan_id=plan.plan_id,
        action_id=action.action_id,
        attempt_id="attempt-1",
        reserved_at="2026-09-21T17:59:50+00:00",
    )
    provider_ref = None
    if bind_provider_ref:
        provider_ref = ledger.bind_provider_order_reference(
            attempt_id="attempt-1",
            provider_id="betfair",
        )
    ledger.mark_submitted(
        "attempt-1",
        submitted_at="2026-09-21T17:59:55+00:00",
    )
    ledger.mark_unknown(
        "attempt-1",
        reason=reason,
        # Deliberately earlier than ledger recorded_at: this caller-provided payload
        # timestamp must NOT shorten the provider visibility horizon.
        observed_at=UNKNOWN_OBSERVED_AT,
    )
    return ledger, action, provider_ref, path


def _absence(observed_at: str, provider_ref: str) -> VerifiedProviderAbsenceEvidence:
    return VerifiedProviderAbsenceEvidence(
        bookmaker_id="betfair",
        account_id="acct-1",
        action_id="action-1",
        adapter_id="betfair-exchange-jsonrpc-readonly",
        adapter_version="1",
        profile_version=1,
        event_id="event-1",
        market_id="1.234",
        selection_id="42",
        observed_at=observed_at,
        current_source_payload_sha256="1" * 64,
        cleared_source_payload_sha256="2" * 64,
        evidence_id="3" * 64,
        provider_order_ref=provider_ref,
    )


def _effect(observed_at: str, provider_ref: str) -> VerifiedProviderEffectEvidence:
    return VerifiedProviderEffectEvidence(
        bookmaker_id="betfair",
        account_id="acct-1",
        action_id="action-1",
        adapter_id="betfair-exchange-jsonrpc-readonly",
        adapter_version="1",
        profile_version=1,
        event_id="event-1",
        market_id="1.234",
        selection_id="42",
        external_receipt_id="bet-1",
        observed_at=observed_at,
        source_payload_sha256="4" * 64,
        status=AcknowledgementStatus.ACCEPTED,
        accepted_odds=Decimal("2.0"),
        accepted_stake=Decimal("10"),
        evidence_id="5" * 64,
        provider_order_ref=provider_ref,
    )


def _resolve(monkeypatch, ledger, action, provider_ref, evidence):
    calls = []

    def fake_verify(actual_action, profile, **kwargs):
        calls.append((actual_action, profile, kwargs))
        return evidence

    monkeypatch.setattr(timeout_resolution, "verify_betfair_provider_state", fake_verify)
    result = timeout_resolution.resolve_betfair_timeout_provider_state(
        ledger,
        action,
        object(),
        attempt_id="attempt-1",
        expected_profile_sha256="a" * 64,
        readback=object(),
    )
    assert calls[0][0] is action
    assert calls[0][2]["expected_provider_order_ref"] == provider_ref
    return result


def test_complete_empty_before_visibility_horizon_stays_indeterminate(
    tmp_path, monkeypatch
) -> None:
    ledger, action, provider_ref, _ = _ledger_with_timeout(tmp_path, monkeypatch)
    assert provider_ref is not None
    result = _resolve(
        monkeypatch,
        ledger,
        action,
        provider_ref,
        _absence("2026-09-21T18:00:14.999999+00:00", provider_ref),
    )

    assert result.kind is timeout_resolution.BetfairTimeoutResolutionKind.INDETERMINATE_BEFORE_VISIBILITY_HORIZON
    assert result.definitive is False
    assert result.timeout_boundary_at == LEDGER_TIMEOUT_BOUNDARY
    assert result.visibility_deadline == "2026-09-21T18:00:15+00:00"
    assert result.evidence is None


def test_complete_empty_exactly_at_visibility_horizon_can_issue_absence(
    tmp_path, monkeypatch
) -> None:
    ledger, action, provider_ref, _ = _ledger_with_timeout(tmp_path, monkeypatch)
    assert provider_ref is not None
    evidence = _absence("2026-09-21T18:00:15+00:00", provider_ref)
    result = _resolve(monkeypatch, ledger, action, provider_ref, evidence)

    assert result.kind is timeout_resolution.BetfairTimeoutResolutionKind.ABSENT_AFTER_VISIBILITY_HORIZON
    assert result.definitive is True
    assert result.evidence is evidence


def test_provider_effect_wins_before_visibility_horizon(tmp_path, monkeypatch) -> None:
    ledger, action, provider_ref, _ = _ledger_with_timeout(tmp_path, monkeypatch)
    assert provider_ref is not None
    evidence = _effect("2026-09-21T18:00:01+00:00", provider_ref)
    result = _resolve(monkeypatch, ledger, action, provider_ref, evidence)

    assert result.kind is timeout_resolution.BetfairTimeoutResolutionKind.EFFECT_PRESENT
    assert result.definitive is True
    assert result.evidence is evidence


def test_caller_unknown_observed_at_cannot_shorten_durable_horizon(
    tmp_path, monkeypatch
) -> None:
    ledger, action, provider_ref, _ = _ledger_with_timeout(tmp_path, monkeypatch)
    assert provider_ref is not None
    # This is >15s after caller payload observed_at (17:59:57) but still <15s
    # after the ledger-owned recorded_at boundary (18:00:00).
    result = _resolve(
        monkeypatch,
        ledger,
        action,
        provider_ref,
        _absence("2026-09-21T18:00:12.500000+00:00", provider_ref),
    )
    assert result.kind is timeout_resolution.BetfairTimeoutResolutionKind.INDETERMINATE_BEFORE_VISIBILITY_HORIZON
    assert result.evidence is None


def test_restart_preserves_original_visibility_deadline(tmp_path, monkeypatch) -> None:
    ledger, action, provider_ref, path = _ledger_with_timeout(tmp_path, monkeypatch)
    assert provider_ref is not None
    original_sha = ledger.verified_snapshot().sha256

    restarted = RealExecutionLedger(path)
    evidence = _absence("2026-09-21T18:00:14+00:00", provider_ref)
    result = _resolve(monkeypatch, restarted, action, provider_ref, evidence)

    assert result.visibility_deadline == "2026-09-21T18:00:15+00:00"
    assert result.ledger_snapshot_sha256 == original_sha
    assert result.evidence is None


def test_stale_readback_before_durable_timeout_boundary_fails_closed(
    tmp_path, monkeypatch
) -> None:
    ledger, action, provider_ref, _ = _ledger_with_timeout(tmp_path, monkeypatch)
    assert provider_ref is not None
    with pytest.raises(
        timeout_resolution.BetfairTimeoutResolutionError,
        match="predates durable ambiguous placement boundary",
    ):
        _resolve(
            monkeypatch,
            ledger,
            action,
            provider_ref,
            _absence("2026-09-21T17:59:59+00:00", provider_ref),
        )


def test_noncanonical_unknown_reason_cannot_mint_timeout_authority(
    tmp_path, monkeypatch
) -> None:
    ledger, action, provider_ref, _ = _ledger_with_timeout(
        tmp_path,
        monkeypatch,
        reason="process_restart",
    )
    assert provider_ref is not None
    monkeypatch.setattr(
        timeout_resolution,
        "verify_betfair_provider_state",
        lambda *args, **kwargs: pytest.fail("verifier must not run"),
    )
    with pytest.raises(
        timeout_resolution.BetfairTimeoutResolutionError,
        match="not a canonical ambiguous Betfair",
    ):
        timeout_resolution.resolve_betfair_timeout_provider_state(
            ledger,
            action,
            object(),
            attempt_id="attempt-1",
            expected_profile_sha256="a" * 64,
            readback=object(),
        )


def test_missing_durable_provider_ref_cannot_mint_timeout_authority(
    tmp_path, monkeypatch
) -> None:
    ledger, action, provider_ref, _ = _ledger_with_timeout(
        tmp_path,
        monkeypatch,
        bind_provider_ref=False,
    )
    assert provider_ref is None
    with pytest.raises(
        timeout_resolution.BetfairTimeoutResolutionError,
        match="lacks durable provider order reference",
    ):
        timeout_resolution.resolve_betfair_timeout_provider_state(
            ledger,
            action,
            object(),
            attempt_id="attempt-1",
            expected_profile_sha256="a" * 64,
            readback=object(),
        )


def test_visibility_horizon_is_fixed_provider_constant() -> None:
    assert timeout_resolution.BETFAIR_TIMEOUT_VISIBILITY_HORIZON_SECONDS == 15
