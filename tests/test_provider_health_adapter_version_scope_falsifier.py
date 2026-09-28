import pytest

from autosport.bookmaker_capability import (
    BookmakerCapability,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
)
from autosport.provider_health import (
    ProviderHealthEvidenceError,
    detect_capability_change,
)


_T0 = "2026-09-21T10:00:00+00:00"
_T1 = "2026-09-21T10:05:00+00:00"
_T2 = "2026-09-21T10:06:00+00:00"
_HASH = "a" * 64


def _profile(
    profile_version: int,
    *,
    adapter_version: str,
    observed_at: str,
    live_state: BookmakerCapabilityState,
) -> BookmakerCapabilityProfile:
    return BookmakerCapabilityProfile(
        venue_id="betfair",
        account_id="acct-1",
        adapter_id="adapter-1",
        adapter_version=adapter_version,
        profile_version=profile_version,
        facts=(
            BookmakerCapabilityFact(
                BookmakerCapability.LIVE_QUOTES_READ,
                live_state,
            ),
        ),
        observed_at=observed_at,
        source_ref=f"profile-{profile_version}",
        source_payload_sha256=_HASH,
    )


def test_capability_drift_cannot_cross_adapter_version_scope() -> None:
    previous = _profile(
        1,
        adapter_version="1.0",
        observed_at=_T0,
        live_state=BookmakerCapabilityState.SUPPORTED,
    )
    current = _profile(
        2,
        adapter_version="2.0",
        observed_at=_T1,
        live_state=BookmakerCapabilityState.UNSUPPORTED,
    )

    # A changed adapter version is a different exact integration surface, not a
    # monotonic revision of one exact adapter scope. Treating this as ordinary
    # capability drift can incorrectly project requalification semantics across
    # an implementation boundary whose capabilities must be established anew.
    with pytest.raises(ProviderHealthEvidenceError):
        detect_capability_change(
            previous,
            current,
            since=_T1,
            observed_at=_T2,
            reason="adapter implementation changed with capability delta",
            source_ref="capability-reprobe",
            source_payload_sha256="b" * 64,
        )
