"""Plan 4 Section 1: offline qualification of provider and governance isolation.

This test uses no bookmaker network, credentials or execution transport.
"""

import pytest

from autosport.bookmaker_capability import (
    BookmakerCapability,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
    UnknownBookmakerCapability,
    UnsupportedBookmakerCapability,
)
from autosport.bookmaker_capability_registry import (
    BookmakerCapabilityRegistry,
    BookmakerGovernanceEvidence,
    GovernancePermissionState,
)

_TS = "2026-10-08T09:00:00+00:00"
_SHA = "a" * 64


def _profile(venue, account, adapter, fact):
    return BookmakerCapabilityProfile(
        venue_id=venue,
        account_id=account,
        adapter_id=adapter,
        adapter_version="1.0",
        profile_version=1,
        facts=(fact,),
        observed_at=_TS,
        source_ref=f"fixture:{venue}:{adapter}",
        source_payload_sha256=_SHA,
    )


def _governance(venue, account, permission):
    return BookmakerGovernanceEvidence(
        venue_id=venue,
        account_id=account,
        jurisdiction="SK",
        terms_version="fixture-v1",
        automation_permission=permission,
        observed_at=_TS,
        source_ref=f"terms-fixture:{venue}",
        source_payload_sha256=_SHA,
    )


def test_provider_scopes_and_governance_remain_independent_after_restart(tmp_path):
    path = tmp_path / "bookmaker capability registry кирилиця.json"
    registry = BookmakerCapabilityRegistry(path)
    unsupported = _profile(
        "provider-unsupported", "account-a", "api-a",
        BookmakerCapabilityFact(
            BookmakerCapability.LIVE_QUOTES_READ,
            BookmakerCapabilityState.UNSUPPORTED,
        ),
    )
    supported = _profile(
        "provider-supported", "account-b", "api-b",
        BookmakerCapabilityFact(
            BookmakerCapability.LIVE_QUOTES_READ,
            BookmakerCapabilityState.SUPPORTED,
        ),
    )
    prohibited = _governance(
        "provider-unsupported", "account-a",
        GovernancePermissionState.PROHIBITED,
    )
    permitted = _governance(
        "provider-supported", "account-b",
        GovernancePermissionState.PERMITTED,
    )

    assert registry.register_profile(unsupported)
    assert registry.register_profile(supported)
    assert registry.register_governance(prohibited)
    assert registry.register_governance(permitted)

    # A replay is a no-op, not a new permission or version.
    assert registry.register_profile(unsupported) is False
    assert registry.register_governance(permitted) is False

    reopened = BookmakerCapabilityRegistry(path)
    a = reopened.latest_profile(
        "provider-unsupported", "account-a", "api-a"
    )
    b = reopened.latest_profile(
        "provider-supported", "account-b", "api-b"
    )
    assert a == unsupported and b == supported
    assert a.profile_id != b.profile_id

    with pytest.raises(UnsupportedBookmakerCapability):
        a.require(BookmakerCapability.LIVE_QUOTES_READ)
    with pytest.raises(UnknownBookmakerCapability):
        b.require(BookmakerCapability.PLACE_BET)
    b.require(BookmakerCapability.LIVE_QUOTES_READ)

    # An unsupported provider and an unknown capability must not erase
    # an independent, supported read-only provider profile.
    assert reopened.latest_profile(
        "provider-unsupported", "account-b", "api-b"
    ) is None
    assert reopened.latest_profile(
        "provider-supported", "account-b", "api-a"
    ) is None
    assert reopened.latest_profile("unregistered", "account-c", "api-c") is None

    assert reopened.governance_history(
        "provider-unsupported", "account-a"
    ) == (prohibited,)
    assert reopened.governance_history(
        "provider-supported", "account-b"
    ) == (permitted,)
    assert reopened.governance_history(
        "provider-supported", "account-a"
    ) == ()

    # Legal/terms metadata is observational and must never grant a
    # missing technical execution capability.
    with pytest.raises(UnknownBookmakerCapability):
        b.require(BookmakerCapability.PLACE_BET)
