from __future__ import annotations

from decimal import Decimal

import pytest

import autosport.betfair_timeout_reconciliation as timeout_resolution
from autosport.real_execution_ledger import AcknowledgementStatus
from autosport.supervised_provider_evidence import (
    VerifiedProviderAbsenceEvidence,
    VerifiedProviderEffectEvidence,
)


TIMEOUT_AT = "2026-09-21T18:00:00+00:00"
PROVIDER_REF = "0123456789abcdef0123456789abcdef"


def _absence(observed_at: str) -> VerifiedProviderAbsenceEvidence:
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
        provider_order_ref=PROVIDER_REF,
    )


def _effect(observed_at: str) -> VerifiedProviderEffectEvidence:
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
        provider_order_ref=PROVIDER_REF,
    )


def _resolve(monkeypatch, evidence):
    calls = []

    def fake_verify(action, profile, **kwargs):
        calls.append((action, profile, kwargs))
        return evidence

    monkeypatch.setattr(timeout_resolution, "verify_betfair_provider_state", fake_verify)
    result = timeout_resolution.resolve_betfair_timeout_provider_state(
        object(),
        object(),
        expected_profile_sha256="a" * 64,
        readback=object(),
        expected_provider_order_ref=PROVIDER_REF,
        timeout_at=TIMEOUT_AT,
    )
    assert calls[0][2]["expected_provider_order_ref"] == PROVIDER_REF
    return result


def test_complete_empty_before_visibility_horizon_stays_indeterminate(monkeypatch) -> None:
    result = _resolve(monkeypatch, _absence("2026-09-21T18:00:14.999999+00:00"))

    assert result.kind is timeout_resolution.BetfairTimeoutResolutionKind.INDETERMINATE_BEFORE_VISIBILITY_HORIZON
    assert result.definitive is False
    assert result.visibility_deadline == "2026-09-21T18:00:15+00:00"
    assert result.evidence is None


def test_complete_empty_exactly_at_visibility_horizon_can_issue_absence(monkeypatch) -> None:
    evidence = _absence("2026-09-21T18:00:15+00:00")
    result = _resolve(monkeypatch, evidence)

    assert result.kind is timeout_resolution.BetfairTimeoutResolutionKind.ABSENT_AFTER_VISIBILITY_HORIZON
    assert result.definitive is True
    assert result.evidence is evidence


def test_complete_empty_after_visibility_horizon_can_issue_absence(monkeypatch) -> None:
    evidence = _absence("2026-09-21T18:00:16+00:00")
    result = _resolve(monkeypatch, evidence)

    assert result.kind is timeout_resolution.BetfairTimeoutResolutionKind.ABSENT_AFTER_VISIBILITY_HORIZON
    assert result.evidence is evidence


def test_provider_effect_wins_before_visibility_horizon(monkeypatch) -> None:
    evidence = _effect("2026-09-21T18:00:01+00:00")
    result = _resolve(monkeypatch, evidence)

    assert result.kind is timeout_resolution.BetfairTimeoutResolutionKind.EFFECT_PRESENT
    assert result.definitive is True
    assert result.evidence is evidence


def test_stale_readback_before_timeout_fails_closed(monkeypatch) -> None:
    with pytest.raises(
        timeout_resolution.BetfairTimeoutResolutionError,
        match="predates ambiguous placement timeout",
    ):
        _resolve(monkeypatch, _absence("2026-09-21T17:59:59+00:00"))


def test_provider_order_ref_is_required_and_exact(monkeypatch) -> None:
    monkeypatch.setattr(
        timeout_resolution,
        "verify_betfair_provider_state",
        lambda *args, **kwargs: pytest.fail("verifier must not run for invalid ref"),
    )
    with pytest.raises(
        timeout_resolution.BetfairTimeoutResolutionError,
        match="expected_provider_order_ref",
    ):
        timeout_resolution.resolve_betfair_timeout_provider_state(
            object(),
            object(),
            expected_profile_sha256="a" * 64,
            readback=object(),
            expected_provider_order_ref="not-provider-hex",
            timeout_at=TIMEOUT_AT,
        )


def test_visibility_horizon_is_fixed_provider_constant() -> None:
    assert timeout_resolution.BETFAIR_TIMEOUT_VISIBILITY_HORIZON_SECONDS == 15
