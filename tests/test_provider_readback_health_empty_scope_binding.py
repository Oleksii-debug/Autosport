from hashlib import sha256

import pytest

from autosport.bookmaker_capability import (
    BookmakerAccountSnapshot,
    BookmakerCapability,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
)
from autosport.provider_readback_health import (
    ProviderReadbackHealthState,
    ProviderReadScopeEvidence,
    classify_provider_readback_health,
)


_OBSERVED = "2026-09-22T12:00:00+00:00"


def _sha(label: str) -> str:
    return sha256(label.encode("utf-8")).hexdigest()


def _empty_open_positions_snapshot() -> BookmakerAccountSnapshot:
    profile = BookmakerCapabilityProfile(
        venue_id="betfair",
        account_id="acct-1",
        adapter_id="betfair-exchange-jsonrpc-readonly",
        adapter_version="1",
        profile_version=1,
        facts=(
            BookmakerCapabilityFact(
                capability=BookmakerCapability.OPEN_POSITIONS_READ,
                state=BookmakerCapabilityState.SUPPORTED,
            ),
        ),
        observed_at=_OBSERVED,
        source_ref="provider-capability-probe",
        source_payload_sha256=_sha("profile"),
    )
    return BookmakerAccountSnapshot(
        profile=profile,
        observed_capabilities=frozenset(
            {BookmakerCapability.OPEN_POSITIONS_READ}
        ),
        observed_at=_OBSERVED,
        open_positions=(),
    )


@pytest.mark.parametrize(
    "response_sha256",
    (_sha("unrelated-empty-response-a"), _sha("unrelated-empty-response-b")),
)
def test_empty_positions_requires_causal_response_binding(
    response_sha256: str,
) -> None:
    """Arbitrary complete-response digests cannot mint authoritative emptiness.

    An empty positions snapshot has no row-level source_payload_sha256. Until the
    canonical provider completeness/acquisition authority is causally bound to
    this exact snapshot, a caller-created complete scope is only an assertion.
    """

    snapshot = _empty_open_positions_snapshot()
    scope = ProviderReadScopeEvidence(
        capability=BookmakerCapability.OPEN_POSITIONS_READ,
        request_scope_sha256=_sha("query-scope"),
        response_sha256=response_sha256,
        observed_at=_OBSERVED,
        complete=True,
    )

    health = classify_provider_readback_health(
        required_capabilities=(BookmakerCapability.OPEN_POSITIONS_READ,),
        evaluated_at="2026-09-22T12:00:05+00:00",
        freshness_limit_seconds=30,
        snapshot=snapshot,
        successful_scopes=(scope,),
    )

    assert health.state is not ProviderReadbackHealthState.FRESH_COMPLETE
    assert health.can_authorize_current_state is False
    assert not health.scope_is_authoritative(
        BookmakerCapability.OPEN_POSITIONS_READ
    )
