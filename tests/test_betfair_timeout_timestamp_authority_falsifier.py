from decimal import Decimal

import autosport.betfair_timeout_reconciliation as timeout_resolution
from autosport.real_execution_ledger import AcknowledgementStatus
from autosport.supervised_provider_evidence import VerifiedProviderAbsenceEvidence


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


def test_caller_cannot_backdate_timeout_origin_to_mint_definitive_absence(
    monkeypatch,
) -> None:
    evidence = _absence("2026-09-21T18:00:05+00:00")
    monkeypatch.setattr(
        timeout_resolution,
        "verify_betfair_provider_state",
        lambda *args, **kwargs: evidence,
    )

    try:
        result = timeout_resolution.resolve_betfair_timeout_provider_state(
            object(),
            object(),
            expected_profile_sha256="a" * 64,
            readback=object(),
            expected_provider_order_ref=PROVIDER_REF,
            timeout_at="2026-09-21T17:59:00+00:00",
        )
    except timeout_resolution.BetfairTimeoutResolutionError:
        return

    assert (
        result.kind
        is timeout_resolution.BetfairTimeoutResolutionKind.INDETERMINATE_BEFORE_VISIBILITY_HORIZON
    )
    assert result.definitive is False
    assert result.evidence is None
