from __future__ import annotations

from dataclasses import replace

import pytest

import autosport._campaign_provider_scope_stable_projection as stable
import autosport.campaign_provider_scope_authority as scope


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
T0 = "2026-09-20T12:00:00Z"
T1 = "2026-09-20T12:05:00Z"


def _projection(**overrides: object) -> scope.CampaignProviderScopeProjection:
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
        "provider_evidence_id": "provider-evidence-t0",
        "provider_source_sha256": SHA_B,
        "venue_id": "betfair",
        "authenticated_account_id": "betfair-account-evidence:" + SHA_C,
        "event_id": "event-1",
        "market_id": "1.23456789",
        "source_interval_start": "2026-09-20T11:59:50Z",
        "source_interval_end": T0,
        "observed_at": T0,
        "available_at": T0,
    }
    values.update(overrides)
    return scope.CampaignProviderScopeProjection(**values)  # type: ignore[arg-type]


class _Ledger:
    def __init__(self, evidence_id: str = "provider-evidence-t0") -> None:
        self.evidence_id = evidence_id

    def provider_evidence_binding(self, attempt_id: str):
        assert attempt_id == "attempt-1"
        return {
            "evidence_id": self.evidence_id,
            "observed_at": T0,
            "source": "betfair",
        }


def _t1_projection() -> scope.CampaignProviderScopeProjection:
    return _projection(
        provider_capture_sha256=SHA_D,
        provider_evidence_id="provider-evidence-t1",
        provider_source_sha256=SHA_D,
        source_interval_end=T1,
        observed_at=T1,
        available_at=T1,
    )


def test_later_reverification_returns_exact_original_t0_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = _projection()
    current = _t1_projection()
    monkeypatch.setattr(stable, "_RAW_RESOLVE", lambda *args, **kwargs: current)

    resolved = scope.resolve_campaign_provider_scope(
        object(),
        session_id="session-1",
        execution_ledger=_Ledger(),
        plan_id="plan-1",
        attempt_id="attempt-1",
        capture=object(),
        expected_applicability_digest=original.applicability_digest,
        expected_projection=original,
    )

    assert resolved is original
    assert resolved.applicability_digest == original.applicability_digest
    scope.assert_campaign_provider_scope_authoritative(resolved)


def test_later_reverification_cannot_change_stable_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = _projection()
    current = replace(_t1_projection(), market_id="1.99999999")
    monkeypatch.setattr(stable, "_RAW_RESOLVE", lambda *args, **kwargs: current)

    with pytest.raises(
        scope.CampaignProviderScopeError,
        match="changed immutable T0 applicability scope",
    ):
        scope.resolve_campaign_provider_scope(
            object(),
            session_id="session-1",
            execution_ledger=_Ledger(),
            plan_id="plan-1",
            attempt_id="attempt-1",
            capture=object(),
            expected_applicability_digest=original.applicability_digest,
            expected_projection=original,
        )


def test_later_reverification_requires_durable_original_t0_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = _projection()
    monkeypatch.setattr(stable, "_RAW_RESOLVE", lambda *args, **kwargs: _t1_projection())

    with pytest.raises(
        scope.CampaignProviderScopeError,
        match="original T0 provider scope evidence is not durable attempt authority",
    ):
        scope.resolve_campaign_provider_scope(
            object(),
            session_id="session-1",
            execution_ledger=_Ledger("other-evidence"),
            plan_id="plan-1",
            attempt_id="attempt-1",
            capture=object(),
            expected_applicability_digest=original.applicability_digest,
            expected_projection=original,
        )


def test_digest_alone_cannot_mint_replacement_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = _projection()
    monkeypatch.setattr(stable, "_RAW_RESOLVE", lambda *args, **kwargs: _t1_projection())

    with pytest.raises(
        scope.CampaignProviderScopeError,
        match="original projection is required",
    ):
        scope.resolve_campaign_provider_scope(
            object(),
            session_id="session-1",
            execution_ledger=_Ledger(),
            plan_id="plan-1",
            attempt_id="attempt-1",
            capture=object(),
            expected_applicability_digest=original.applicability_digest,
        )
