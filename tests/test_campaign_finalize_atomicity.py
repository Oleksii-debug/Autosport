from __future__ import annotations

import pytest

from autosport import campaign_economic_authority as economic_authority
from autosport.campaign_denomination import CampaignDenominationError
from autosport.campaign_economic_authority import FinalizedCampaignAuthority
from autosport.campaign_evidence import (
    CampaignFinalizedError,
    CampaignOutcome,
    CampaignReadiness,
)
from test_campaign_denomination_runtime import _goal, _runtime_provenance
from test_campaign_evidence import PaperCampaignTests


_FINALIZED_AT = "2026-09-11T00:00:00Z"


def _draft_with_goal(currency: str = "EUR"):
    fixture = PaperCampaignTests(
        methodName="test_session_evidence_hash_binds_window_and_metrics"
    )
    fixture.setUp()
    fixture._economic_goal_provenance = _runtime_provenance(_goal(currency=currency))
    campaign = fixture.campaign()
    fixture.add_session(campaign, fixture.session())
    return fixture, campaign


def _finalize(campaign):
    return campaign.finalize(
        outcome=CampaignOutcome.POSITIVE,
        readiness=CampaignReadiness.ELIGIBLE,
        finalized_at=_FINALIZED_AT,
    )


def _assert_retryable_draft(campaign) -> None:
    assert campaign.finalized is False
    assert campaign._sealed is False
    assert campaign.finalized_at is None
    assert campaign.outcome is None
    assert campaign.readiness is None
    assert campaign.campaign_sha256 is None
    assert isinstance(campaign.sessions, list)
    assert len(campaign.sessions) == 1


def test_invalid_currency_issuance_failure_does_not_seal_campaign() -> None:
    fixture, campaign = _draft_with_goal("ZZZ")
    try:
        with pytest.raises(CampaignDenominationError, match="ISO 4217"):
            _finalize(campaign)
        _assert_retryable_draft(campaign)
    finally:
        fixture.doCleanups()


def test_durable_binding_write_failure_leaves_exact_retryable_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture, campaign = _draft_with_goal()
    original_write = economic_authority.atomic_write_json
    calls = 0

    def fail_once(path, payload):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("forced denomination registry publication failure")
        return original_write(path, payload)

    try:
        monkeypatch.setattr(economic_authority, "atomic_write_json", fail_once)
        with pytest.raises(OSError, match="forced denomination"):
            _finalize(campaign)
        _assert_retryable_draft(campaign)

        monkeypatch.setattr(economic_authority, "atomic_write_json", original_write)
        summary = _finalize(campaign)
        assert summary.status == "FINALIZED"
        assert campaign.finalized is True
        assert campaign._sealed is True
        binding = FinalizedCampaignAuthority(campaign).denomination_binding()
        assert binding is not None

        with pytest.raises(CampaignFinalizedError, match="already finalized"):
            _finalize(campaign)
    finally:
        fixture.doCleanups()
