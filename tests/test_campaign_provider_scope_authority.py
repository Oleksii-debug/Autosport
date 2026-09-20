from __future__ import annotations

from dataclasses import replace

import pytest

from autosport.betfair_account_readonly import ADAPTER_ID, ADAPTER_VERSION
from autosport.campaign_provider_scope_authority import (
    CampaignProviderScopeError,
    CampaignProviderScopeProjection,
    VerifiedBetfairProviderScopeCapture,
    assert_provider_scope_capture_authoritative,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64


def _capture(**overrides: object) -> VerifiedBetfairProviderScopeCapture:
    values: dict[str, object] = {
        "venue_id": "betfair",
        "client_account_scope": "client-scope",
        "authenticated_account_id": "betfair-account-evidence:" + SHA_A,
        "account_details_sha256": SHA_A,
        "adapter_id": ADAPTER_ID,
        "adapter_version": ADAPTER_VERSION,
        "action_id": "action-1",
        "event_id": "event-1",
        "market_id": "1.23456789",
        "provider_order_ref": "bet-1",
        "provider_evidence_id": "provider-evidence-1",
        "provider_source_sha256": SHA_B,
        "request_scope_sha256": SHA_C,
        "readback_evidence_sha256": SHA_D,
        "quote_observed_at": "2026-09-20T12:00:00Z",
        "account_observed_at": "2026-09-20T12:00:01Z",
        "readback_observed_at": "2026-09-20T12:00:02Z",
        "source_interval_start": "2026-09-20T12:00:00Z",
        "source_interval_end": "2026-09-20T12:00:02Z",
        "available_at": "2026-09-20T12:00:02Z",
    }
    values.update(overrides)
    return VerifiedBetfairProviderScopeCapture(**values)  # type: ignore[arg-type]


def _projection(**overrides: object) -> CampaignProviderScopeProjection:
    values: dict[str, object] = {
        "campaign_id": "campaign-1",
        "campaign_version": 1,
        "campaign_sha256": SHA_A,
        "session_id": "session-1",
        "run_id": "run-1",
        "session_evidence_id": "session-evidence-1",
        "session_evidence_sha256": SHA_B,
        "run_summary_sha256": SHA_C,
        "decision_id": "decision-1",
        "plan_id": "plan-1",
        "plan_fingerprint": SHA_D,
        "action_id": "action-1",
        "provider_capture_sha256": SHA_A,
        "provider_evidence_id": "provider-evidence-1",
        "provider_source_sha256": SHA_B,
        "venue_id": "betfair",
        "authenticated_account_id": "betfair-account-evidence:" + SHA_C,
        "event_id": "event-1",
        "market_id": "1.23456789",
        "source_interval_start": "2026-09-20T12:00:00Z",
        "source_interval_end": "2026-09-20T12:00:02Z",
        "observed_at": "2026-09-20T12:00:02Z",
        "available_at": "2026-09-20T12:00:02Z",
    }
    values.update(overrides)
    return CampaignProviderScopeProjection(**values)  # type: ignore[arg-type]


def test_authenticated_account_identity_must_be_source_derived() -> None:
    with pytest.raises(
        CampaignProviderScopeError,
        match="authenticated account identity is not source-derived",
    ):
        _capture(authenticated_account_id="caller-account-label")


def test_source_interval_is_mechanically_derived() -> None:
    with pytest.raises(
        CampaignProviderScopeError,
        match="provider scope start is not mechanically derived",
    ):
        _capture(source_interval_start="2026-09-20T11:59:59Z")

    with pytest.raises(
        CampaignProviderScopeError,
        match="provider scope end is not mechanically derived",
    ):
        _capture(source_interval_end="2026-09-20T12:00:03Z")

    with pytest.raises(
        CampaignProviderScopeError,
        match="provider scope availability must equal latest source observation",
    ):
        _capture(available_at="2026-09-20T12:00:03Z")


def test_caller_constructed_capture_cannot_mint_positive_authority() -> None:
    forged = _capture()

    with pytest.raises(
        CampaignProviderScopeError,
        match="was not issued by canonical resolver",
    ):
        assert_provider_scope_capture_authoritative(forged)


def test_capture_identity_binds_authenticated_account_and_external_market() -> None:
    base = _capture()
    changed_account = _capture(
        account_details_sha256=SHA_D,
        authenticated_account_id="betfair-account-evidence:" + SHA_D,
    )
    changed_market = _capture(market_id="1.99999999")
    changed_action = _capture(action_id="action-2")

    assert base.capture_sha256 != changed_account.capture_sha256
    assert base.capture_sha256 != changed_market.capture_sha256
    assert base.capture_sha256 != changed_action.capture_sha256


def test_frozen_capture_copy_is_not_source_authoritative() -> None:
    forged = _capture()
    copied = replace(forged, market_id="1.99999999")

    with pytest.raises(
        CampaignProviderScopeError,
        match="was not issued by canonical resolver",
    ):
        assert_provider_scope_capture_authoritative(copied)


def test_projection_digest_binds_campaign_and_provider_scope() -> None:
    base = _projection()

    assert base.applicability_digest != _projection(
        campaign_id="campaign-2"
    ).applicability_digest
    assert base.applicability_digest != _projection(
        authenticated_account_id="betfair-account-evidence:" + SHA_D
    ).applicability_digest
    assert base.applicability_digest != _projection(
        market_id="1.99999999"
    ).applicability_digest
    assert base.applicability_digest != _projection(
        event_id="event-2"
    ).applicability_digest


def test_projection_provider_key_accepts_only_exact_source_scope() -> None:
    projection = _projection()
    projection.assert_provider_key(
        venue_id="betfair",
        authenticated_account_id="betfair-account-evidence:" + SHA_C,
        market_id="1.23456789",
        event_id="event-1",
    )

    for kwargs in (
        {
            "venue_id": "other",
            "authenticated_account_id": "betfair-account-evidence:" + SHA_C,
            "market_id": "1.23456789",
            "event_id": "event-1",
        },
        {
            "venue_id": "betfair",
            "authenticated_account_id": "betfair-account-evidence:" + SHA_D,
            "market_id": "1.23456789",
            "event_id": "event-1",
        },
        {
            "venue_id": "betfair",
            "authenticated_account_id": "betfair-account-evidence:" + SHA_C,
            "market_id": "1.99999999",
            "event_id": "event-1",
        },
        {
            "venue_id": "betfair",
            "authenticated_account_id": "betfair-account-evidence:" + SHA_C,
            "market_id": "1.23456789",
            "event_id": "event-2",
        },
    ):
        with pytest.raises(
            CampaignProviderScopeError,
            match="is not applicable to campaign",
        ):
            projection.assert_provider_key(**kwargs)
