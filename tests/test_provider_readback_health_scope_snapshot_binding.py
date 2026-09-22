from __future__ import annotations

from decimal import Decimal

from autosport.bookmaker_capability import (
    BookmakerAccountSnapshot,
    BookmakerBalanceObservation,
    BookmakerCapability,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
)
from autosport.provider_readback_health import (
    ProviderReadbackHealthError,
    ProviderReadbackHealthState,
    ProviderReadScopeEvidence,
    classify_provider_readback_health,
)


OBSERVED = "2026-09-21T18:00:00+00:00"
SNAPSHOT_RESPONSE_SHA = "a" * 64
UNRELATED_RESPONSE_SHA = "b" * 64
REQUEST_SCOPE_SHA = "c" * 64


def _profile() -> BookmakerCapabilityProfile:
    return BookmakerCapabilityProfile(
        venue_id="betfair",
        account_id="acct-1",
        adapter_id="betfair-exchange-jsonrpc-readonly",
        adapter_version="1",
        profile_version=1,
        facts=(
            BookmakerCapabilityFact(
                capability=BookmakerCapability.BALANCE_READ,
                state=BookmakerCapabilityState.SUPPORTED,
            ),
        ),
        observed_at=OBSERVED,
        source_ref="provider-capability-probe",
        source_payload_sha256=SNAPSHOT_RESPONSE_SHA,
    )


def _snapshot() -> BookmakerAccountSnapshot:
    return BookmakerAccountSnapshot(
        profile=_profile(),
        observed_capabilities=frozenset({BookmakerCapability.BALANCE_READ}),
        observed_at=OBSERVED,
        balance=BookmakerBalanceObservation(
            venue_id="betfair",
            account_id="acct-1",
            adapter_id="betfair-exchange-jsonrpc-readonly",
            observation_id="balance-1",
            currency="EUR",
            available_balance=Decimal("90"),
            observed_at=OBSERVED,
            source_payload_sha256=SNAPSHOT_RESPONSE_SHA,
            total_balance=Decimal("100"),
            total_balance_source_ref="getAccountFunds",
            exposure=Decimal("-10"),
        ),
    )


def _scope(response_sha256: str) -> ProviderReadScopeEvidence:
    return ProviderReadScopeEvidence(
        capability=BookmakerCapability.BALANCE_READ,
        request_scope_sha256=REQUEST_SCOPE_SHA,
        response_sha256=response_sha256,
        observed_at=OBSERVED,
        complete=True,
    )


def _classify(response_sha256: str):
    return classify_provider_readback_health(
        required_capabilities=(BookmakerCapability.BALANCE_READ,),
        evaluated_at="2026-09-21T18:00:05+00:00",
        freshness_limit_seconds=30,
        snapshot=_snapshot(),
        successful_scopes=(_scope(response_sha256),),
    )


def test_matching_balance_response_can_authorize_fresh_complete() -> None:
    health = _classify(SNAPSHOT_RESPONSE_SHA)

    assert health.state is ProviderReadbackHealthState.FRESH_COMPLETE
    assert health.can_authorize_current_state
    assert health.scope_is_authoritative(BookmakerCapability.BALANCE_READ)


def test_unrelated_complete_response_cannot_authorize_balance_snapshot() -> None:
    try:
        health = _classify(UNRELATED_RESPONSE_SHA)
    except ProviderReadbackHealthError:
        # A canonical repair may reject the incoherent evidence at issuance time.
        return

    assert health.state is not ProviderReadbackHealthState.FRESH_COMPLETE
    assert not health.can_authorize_current_state
    assert not health.scope_is_authoritative(BookmakerCapability.BALANCE_READ)
