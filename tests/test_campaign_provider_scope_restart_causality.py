from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

import autosport._campaign_provider_scope_devapp_identity as devapp
from autosport.betfair_account_readonly import ADAPTER_ID, ADAPTER_VERSION
from autosport.campaign_provider_scope_authority import (
    CampaignProviderScopeError,
    VerifiedBetfairProviderScopeCapture,
)
from autosport.real_execution_ledger import ExecutionAction, ExternalReceiptIdentity


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
T0 = datetime(2026, 9, 20, 12, 0, 0, tzinfo=timezone.utc)


def _action() -> ExecutionAction:
    return ExecutionAction(
        action_id="action-1",
        bookmaker_id="betfair",
        account_id="acct-1",
        event_id="event-1",
        market_id="1.23456789",
        selection_id="42",
        side="BACK",
        requested_odds=Decimal("2"),
        requested_stake=Decimal("1"),
        quote_id="quote-1",
        quote_observed_at="2026-09-20T11:59:50Z",
        expires_at="2026-09-20T12:10:00Z",
    )


def _capture(
    *,
    evidence_id: str = SHA_A,
    readback_observed_at: str = "2026-09-20T12:05:00Z",
) -> VerifiedBetfairProviderScopeCapture:
    return VerifiedBetfairProviderScopeCapture(
        venue_id="betfair",
        client_account_scope="acct-1",
        authenticated_account_id="betfair-account-evidence:" + SHA_B,
        account_details_sha256=SHA_B,
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        action_id="action-1",
        event_id="event-1",
        market_id="1.23456789",
        provider_order_ref="action-1",
        provider_evidence_id=evidence_id,
        provider_source_sha256=SHA_C,
        request_scope_sha256=SHA_D,
        readback_evidence_sha256=SHA_A,
        quote_observed_at="2026-09-20T11:59:50Z",
        account_observed_at=readback_observed_at,
        readback_observed_at=readback_observed_at,
        source_interval_start="2026-09-20T11:59:50Z",
        source_interval_end=readback_observed_at,
        available_at=readback_observed_at,
    )


class _Ledger:
    def __init__(
        self,
        *,
        binding_evidence_id: str = SHA_D,
        binding_observed_at: str = "2026-09-20T11:59:59Z",
        receipt_id: str = "bet-1",
    ) -> None:
        self._binding = {
            "evidence_id": binding_evidence_id,
            "observed_at": binding_observed_at,
            "source": "betfair",
        }
        self._receipt_id = receipt_id

    def provider_evidence_binding(self, attempt_id: str):
        assert attempt_id == "attempt-1"
        return dict(self._binding)

    def saga(self, plan_id: str):
        assert plan_id == "plan-1"
        return SimpleNamespace(
            receipts={
                ExternalReceiptIdentity("betfair", "acct-1", self._receipt_id):
                "attempt-1"
            }
        )


def _bind_effect(
    capture: VerifiedBetfairProviderScopeCapture,
    *,
    receipt_id: str = "bet-1",
    selection_id: str = "42",
) -> None:
    devapp._CAPTURE_EFFECT_IDENTITIES[capture] = devapp._VerifiedEffectIdentity(
        external_receipt_id=receipt_id,
        selection_id=selection_id,
        status="ACCEPTED",
        accepted_odds="2",
        accepted_stake="1",
    )


def test_restart_reverification_accepts_later_source_read_without_backdating() -> None:
    capture = _capture()
    _bind_effect(capture)

    devapp._assert_durable_effect_reverification(
        _Ledger(),
        plan_id="plan-1",
        attempt_id="attempt-1",
        action=_action(),
        capture=capture,
        campaign_as_of=T0,
    )

    assert capture.available_at == "2026-09-20T12:05:00Z"
    assert capture.source_interval_end == "2026-09-20T12:05:00Z"


def test_restart_reverification_rejects_changed_external_receipt() -> None:
    capture = _capture()
    _bind_effect(capture, receipt_id="bet-other")

    with pytest.raises(
        CampaignProviderScopeError,
        match="external receipt is not durable attempt identity",
    ):
        devapp._assert_durable_effect_reverification(
            _Ledger(receipt_id="bet-1"),
            plan_id="plan-1",
            attempt_id="attempt-1",
            action=_action(),
            capture=capture,
            campaign_as_of=T0,
        )


def test_restart_reverification_rejects_selection_swap() -> None:
    capture = _capture()
    _bind_effect(capture, selection_id="99")

    with pytest.raises(
        CampaignProviderScopeError,
        match="selection conflicts with execution action",
    ):
        devapp._assert_durable_effect_reverification(
            _Ledger(),
            plan_id="plan-1",
            attempt_id="attempt-1",
            action=_action(),
            capture=capture,
            campaign_as_of=T0,
        )


def test_changed_evidence_before_or_at_campaign_cutoff_cannot_be_reinterpreted() -> None:
    capture = _capture(readback_observed_at="2026-09-20T12:00:00Z")
    _bind_effect(capture)

    with pytest.raises(
        CampaignProviderScopeError,
        match="drifted before campaign cutoff",
    ):
        devapp._assert_durable_effect_reverification(
            _Ledger(),
            plan_id="plan-1",
            attempt_id="attempt-1",
            action=_action(),
            capture=capture,
            campaign_as_of=T0,
        )


def test_same_capture_binding_remains_valid_without_restart_receipt_lookup() -> None:
    capture = _capture(
        evidence_id=SHA_A,
        readback_observed_at="2026-09-20T11:59:59Z",
    )
    ledger = _Ledger(
        binding_evidence_id=SHA_A,
        binding_observed_at="2026-09-20T11:59:59Z",
        receipt_id="different-receipt-does-not-matter-for-exact-binding",
    )

    devapp._assert_durable_effect_reverification(
        ledger,
        plan_id="plan-1",
        attempt_id="attempt-1",
        action=_action(),
        capture=capture,
        campaign_as_of=T0,
    )
