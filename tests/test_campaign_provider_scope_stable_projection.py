from __future__ import annotations

from dataclasses import replace
from pathlib import Path

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
    def __init__(
        self,
        root: Path,
        evidence_id: str = "provider-evidence-t0",
    ) -> None:
        self.path = root / "execution.jsonl"
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


def _resolve(
    ledger: _Ledger,
    *,
    expected_applicability_digest: str | None = None,
    expected_projection: scope.CampaignProviderScopeProjection | None = None,
) -> scope.CampaignProviderScopeProjection:
    return scope.resolve_campaign_provider_scope(
        object(),
        session_id="session-1",
        execution_ledger=ledger,
        plan_id="plan-1",
        attempt_id="attempt-1",
        capture=object(),
        expected_applicability_digest=expected_applicability_digest,
        expected_projection=expected_projection,
    )


def _issue_t0(
    monkeypatch: pytest.MonkeyPatch,
    ledger: _Ledger,
    original: scope.CampaignProviderScopeProjection,
) -> None:
    monkeypatch.setattr(
        stable,
        "_RAW_RESOLVE",
        lambda *args, **kwargs: original,
    )
    resolved = _resolve(ledger)
    assert resolved == original
    scope.assert_campaign_provider_scope_authoritative(resolved)


def test_later_reverification_returns_durable_original_t0_projection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    original = _projection()
    ledger = _Ledger(tmp_path)
    _issue_t0(monkeypatch, ledger, original)
    monkeypatch.setattr(
        stable,
        "_RAW_RESOLVE",
        lambda *args, **kwargs: _t1_projection(),
    )

    resolved = _resolve(
        ledger,
        expected_applicability_digest=original.applicability_digest,
    )

    assert resolved == original
    assert resolved.applicability_digest == original.applicability_digest
    scope.assert_campaign_provider_scope_authoritative(resolved)
    receipt_path = stable._receipt_path(ledger)
    assert receipt_path.exists()
    assert receipt_path.read_bytes().endswith(b"\n")


def test_later_reverification_cannot_change_stable_scope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    original = _projection()
    ledger = _Ledger(tmp_path)
    _issue_t0(monkeypatch, ledger, original)
    current = replace(_t1_projection(), market_id="1.99999999")
    monkeypatch.setattr(stable, "_RAW_RESOLVE", lambda *args, **kwargs: current)

    with pytest.raises(
        scope.CampaignProviderScopeError,
        match="changed immutable T0 applicability scope",
    ):
        _resolve(
            ledger,
            expected_applicability_digest=original.applicability_digest,
        )


def test_later_reverification_requires_durable_original_t0_binding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    original = _projection()
    initial_ledger = _Ledger(tmp_path)
    _issue_t0(monkeypatch, initial_ledger, original)
    monkeypatch.setattr(
        stable,
        "_RAW_RESOLVE",
        lambda *args, **kwargs: _t1_projection(),
    )

    with pytest.raises(
        scope.CampaignProviderScopeError,
        match="original T0 provider scope evidence is not durable attempt authority",
    ):
        _resolve(
            _Ledger(tmp_path, "other-evidence"),
            expected_applicability_digest=original.applicability_digest,
        )


def test_digest_alone_reloads_product_owned_t0_projection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    original = _projection()
    ledger = _Ledger(tmp_path)
    _issue_t0(monkeypatch, ledger, original)
    monkeypatch.setattr(
        stable,
        "_RAW_RESOLVE",
        lambda *args, **kwargs: _t1_projection(),
    )

    resolved = _resolve(
        ledger,
        expected_applicability_digest=original.applicability_digest,
    )

    assert resolved == original
    assert resolved.provider_evidence_id == "provider-evidence-t0"
    assert resolved.observed_at == T0


def test_caller_forged_old_projection_cannot_replace_durable_t0(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    original = _projection()
    ledger = _Ledger(tmp_path)
    _issue_t0(monkeypatch, ledger, original)
    forged = replace(
        original,
        provider_capture_sha256=SHA_D,
        provider_source_sha256=SHA_D,
    )
    monkeypatch.setattr(
        stable,
        "_RAW_RESOLVE",
        lambda *args, **kwargs: _t1_projection(),
    )

    with pytest.raises(
        scope.CampaignProviderScopeError,
        match="caller-supplied original T0 projection is not durable authority",
    ):
        _resolve(
            ledger,
            expected_applicability_digest=original.applicability_digest,
            expected_projection=forged,
        )


def test_caller_projection_cannot_mint_missing_durable_t0_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    alleged_original = _projection()
    ledger = _Ledger(tmp_path)
    monkeypatch.setattr(
        stable,
        "_RAW_RESOLVE",
        lambda *args, **kwargs: _t1_projection(),
    )

    with pytest.raises(
        scope.CampaignProviderScopeError,
        match="durable original T0 provider scope receipt is unavailable",
    ):
        _resolve(
            ledger,
            expected_applicability_digest=alleged_original.applicability_digest,
            expected_projection=alleged_original,
        )


def test_t0_receipt_tamper_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    original = _projection()
    ledger = _Ledger(tmp_path)
    _issue_t0(monkeypatch, ledger, original)
    receipt_path = stable._receipt_path(ledger)
    raw = receipt_path.read_text(encoding="utf-8")
    receipt_path.write_text(
        raw.replace(
            '"market_id":"1.23456789"',
            '"market_id":"1.99999999"',
        ),
        encoding="utf-8",
        newline="\n",
    )
    monkeypatch.setattr(
        stable,
        "_RAW_RESOLVE",
        lambda *args, **kwargs: _t1_projection(),
    )

    with pytest.raises(
        scope.CampaignProviderScopeError,
        match="receipt integrity failed",
    ):
        _resolve(
            ledger,
            expected_applicability_digest=original.applicability_digest,
        )
