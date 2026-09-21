from __future__ import annotations

from dataclasses import replace

import pytest

from autosport.bookmaker_capability import (
    BookmakerCapability,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
)
from autosport.bookmaker_capability_registry import BookmakerCapabilityRegistry
from autosport.bookmaker_integration_boundary import (
    BookmakerIntegrationKind,
    bind_bookmaker_integration,
)
from autosport.provider_fallback_policy import (
    ProviderFallbackDisposition,
    ProviderFallbackPolicyError,
    ProviderOperationalObservation,
    ProviderOperationalState,
    ProviderRouteIdentity,
    resolve_readonly_provider_route,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
T0 = "2026-09-21T10:00:00+00:00"
T1 = "2026-09-21T10:01:00+00:00"
T2 = "2026-09-21T10:02:00+00:00"
T3 = "2026-09-21T10:03:00+00:00"
T4 = "2026-09-21T10:04:00+00:00"


def _profile(
    *,
    venue: str,
    account: str,
    adapter: str,
    version: int = 1,
    capability: BookmakerCapability = BookmakerCapability.LIVE_QUOTES_READ,
    state: BookmakerCapabilityState = BookmakerCapabilityState.SUPPORTED,
    observed_at: str = T0,
    source_sha: str = SHA_A,
) -> BookmakerCapabilityProfile:
    return BookmakerCapabilityProfile(
        venue_id=venue,
        account_id=account,
        adapter_id=adapter,
        adapter_version=f"{version}.0",
        profile_version=version,
        facts=(BookmakerCapabilityFact(capability=capability, state=state),),
        observed_at=observed_at,
        source_ref=f"provider:{venue}:profile:{version}",
        source_payload_sha256=source_sha,
    )


def _route(profile: BookmakerCapabilityProfile) -> ProviderRouteIdentity:
    return ProviderRouteIdentity(
        venue_id=profile.venue_id,
        account_id=profile.account_id,
        adapter_id=profile.adapter_id,
        profile_id=profile.profile_id,
    )


def _integration(
    profile: BookmakerCapabilityProfile,
    *,
    kind: BookmakerIntegrationKind = BookmakerIntegrationKind.OFFICIAL_API,
    observed_at: str = T1,
):
    return bind_bookmaker_integration(
        profile,
        integration_kind=kind,
        observed_at=observed_at,
        source_ref=f"integration:{profile.venue_id}:{profile.adapter_id}",
        source_payload_sha256=SHA_B,
    )


def _operational(
    profile: BookmakerCapabilityProfile,
    *,
    state: ProviderOperationalState,
    observed_at: str = T2,
) -> ProviderOperationalObservation:
    return ProviderOperationalObservation(
        route=_route(profile),
        state=state,
        observed_at=observed_at,
        source_ref=f"runtime:{profile.venue_id}:{profile.adapter_id}",
        source_payload_sha256=SHA_C,
    )


def _registry(tmp_path, *profiles: BookmakerCapabilityProfile) -> BookmakerCapabilityRegistry:
    registry = BookmakerCapabilityRegistry(tmp_path / "bookmaker_capabilities.json")
    for profile in profiles:
        assert registry.register_profile(profile)
    return registry


def _resolve(
    registry: BookmakerCapabilityRegistry,
    primary: BookmakerCapabilityProfile,
    *,
    primary_state: ProviderOperationalState,
    fallback: BookmakerCapabilityProfile | None = None,
    fallback_state: ProviderOperationalState = ProviderOperationalState.HEALTHY,
    capability: BookmakerCapability = BookmakerCapability.LIVE_QUOTES_READ,
    fallback_kind: BookmakerIntegrationKind = BookmakerIntegrationKind.OFFICIAL_API,
):
    fallback_args = {}
    if fallback is not None:
        fallback_args = {
            "fallback_route": _route(fallback),
            "fallback_integration": _integration(fallback, kind=fallback_kind),
            "fallback_operational": _operational(fallback, state=fallback_state),
        }
    return resolve_readonly_provider_route(
        registry=registry,
        required_capability=capability,
        primary_route=_route(primary),
        primary_integration=_integration(primary),
        primary_operational=_operational(primary, state=primary_state),
        decided_at=T3,
        **fallback_args,
    )


def test_healthy_supported_primary_is_selected_without_authority_widening(tmp_path):
    primary = _profile(venue="primary", account="acct-p", adapter="api-p")
    decision = _resolve(
        _registry(tmp_path, primary),
        primary,
        primary_state=ProviderOperationalState.HEALTHY,
    )

    assert decision.disposition is ProviderFallbackDisposition.USE_PRIMARY_READ
    assert decision.selected_profile_id == primary.profile_id
    assert decision.reason == "PRIMARY_HEALTHY_SUPPORTED"
    assert decision.source_quality_authority is False
    assert decision.execution_authority is False
    assert decision.downstream_source_quality_required is True


@pytest.mark.parametrize(
    "state",
    [ProviderOperationalState.DEGRADED, ProviderOperationalState.UNAVAILABLE],
)
def test_degraded_or_unavailable_primary_can_use_healthy_supported_fallback(tmp_path, state):
    primary = _profile(venue="primary", account="acct-p", adapter="api-p")
    fallback = _profile(
        venue="fallback", account="acct-f", adapter="api-f", source_sha=SHA_D
    )
    decision = _resolve(
        _registry(tmp_path, primary, fallback),
        primary,
        primary_state=state,
        fallback=fallback,
    )

    assert decision.disposition is ProviderFallbackDisposition.USE_FALLBACK_READ
    assert decision.selected_profile_id == fallback.profile_id
    assert decision.reason == f"PRIMARY_{state.value}_FALLBACK_HEALTHY_SUPPORTED"
    assert decision.source_quality_authority is False
    assert decision.execution_authority is False
    assert decision.downstream_source_quality_required is True


def test_browser_fallback_remains_only_a_technical_route(tmp_path):
    primary = _profile(venue="primary", account="acct-p", adapter="api-p")
    fallback = _profile(
        venue="fallback", account="acct-f", adapter="browser-f", source_sha=SHA_D
    )
    decision = _resolve(
        _registry(tmp_path, primary, fallback),
        primary,
        primary_state=ProviderOperationalState.UNAVAILABLE,
        fallback=fallback,
        fallback_kind=BookmakerIntegrationKind.BROWSER_AUTOMATION,
    )

    assert decision.disposition is ProviderFallbackDisposition.USE_FALLBACK_READ
    assert decision.downstream_source_quality_required is True
    assert decision.source_quality_authority is False


@pytest.mark.parametrize(
    "primary_capability, expected_reason",
    [
        (
            BookmakerCapabilityState.UNSUPPORTED,
            "PRIMARY_HEALTHY_CAPABILITY_UNSUPPORTED",
        ),
        (BookmakerCapabilityState.UNKNOWN, "PRIMARY_HEALTHY_CAPABILITY_UNKNOWN"),
    ],
)
def test_healthy_primary_capability_gap_does_not_silently_switch_sources(
    tmp_path, primary_capability, expected_reason
):
    if primary_capability is BookmakerCapabilityState.UNKNOWN:
        primary = _profile(
            venue="primary",
            account="acct-p",
            adapter="api-p",
            capability=BookmakerCapability.BALANCE_READ,
        )
    else:
        primary = _profile(
            venue="primary",
            account="acct-p",
            adapter="api-p",
            state=primary_capability,
        )
    fallback = _profile(
        venue="fallback", account="acct-f", adapter="api-f", source_sha=SHA_D
    )
    decision = _resolve(
        _registry(tmp_path, primary, fallback),
        primary,
        primary_state=ProviderOperationalState.HEALTHY,
        fallback=fallback,
    )

    assert decision.disposition is ProviderFallbackDisposition.ABSTAIN
    assert decision.selected_profile_id is None
    assert decision.reason == expected_reason


def test_degraded_primary_without_fallback_abstains(tmp_path):
    primary = _profile(venue="primary", account="acct-p", adapter="api-p")
    decision = _resolve(
        _registry(tmp_path, primary),
        primary,
        primary_state=ProviderOperationalState.DEGRADED,
    )

    assert decision.disposition is ProviderFallbackDisposition.ABSTAIN
    assert decision.reason == "PRIMARY_DEGRADED_NO_FALLBACK"


@pytest.mark.parametrize(
    "state",
    [ProviderOperationalState.DEGRADED, ProviderOperationalState.UNAVAILABLE],
)
def test_nonhealthy_fallback_abstains(tmp_path, state):
    primary = _profile(venue="primary", account="acct-p", adapter="api-p")
    fallback = _profile(
        venue="fallback", account="acct-f", adapter="api-f", source_sha=SHA_D
    )
    decision = _resolve(
        _registry(tmp_path, primary, fallback),
        primary,
        primary_state=ProviderOperationalState.UNAVAILABLE,
        fallback=fallback,
        fallback_state=state,
    )

    assert decision.disposition is ProviderFallbackDisposition.ABSTAIN
    assert decision.reason == f"FALLBACK_{state.value}"


@pytest.mark.parametrize(
    "fallback_state, expected_reason",
    [
        (BookmakerCapabilityState.UNSUPPORTED, "FALLBACK_CAPABILITY_UNSUPPORTED"),
        (BookmakerCapabilityState.UNKNOWN, "FALLBACK_CAPABILITY_UNKNOWN"),
    ],
)
def test_fallback_must_durably_support_exact_required_capability(
    tmp_path, fallback_state, expected_reason
):
    primary = _profile(venue="primary", account="acct-p", adapter="api-p")
    if fallback_state is BookmakerCapabilityState.UNKNOWN:
        fallback = _profile(
            venue="fallback",
            account="acct-f",
            adapter="api-f",
            capability=BookmakerCapability.BALANCE_READ,
            source_sha=SHA_D,
        )
    else:
        fallback = _profile(
            venue="fallback",
            account="acct-f",
            adapter="api-f",
            state=fallback_state,
            source_sha=SHA_D,
        )
    decision = _resolve(
        _registry(tmp_path, primary, fallback),
        primary,
        primary_state=ProviderOperationalState.UNAVAILABLE,
        fallback=fallback,
    )

    assert decision.disposition is ProviderFallbackDisposition.ABSTAIN
    assert decision.reason == expected_reason


@pytest.mark.parametrize(
    "capability",
    [
        BookmakerCapability.PLACE_BET,
        BookmakerCapability.BET_READBACK,
        BookmakerCapability.CASHOUT,
        BookmakerCapability.CANCEL_BET,
    ],
)
def test_execution_or_execution_adjacent_capabilities_are_rejected(tmp_path, capability):
    primary = _profile(venue="primary", account="acct-p", adapter="api-p")
    with pytest.raises(ProviderFallbackPolicyError, match="read-only"):
        _resolve(
            _registry(tmp_path, primary),
            primary,
            primary_state=ProviderOperationalState.HEALTHY,
            capability=capability,
        )


def test_stale_primary_profile_fails_closed_after_registry_advances(tmp_path):
    v1 = _profile(venue="primary", account="acct-p", adapter="api-p", version=1)
    registry = _registry(tmp_path, v1)
    v2 = _profile(
        venue="primary",
        account="acct-p",
        adapter="api-p",
        version=2,
        observed_at=T1,
        source_sha=SHA_D,
    )
    assert registry.register_profile(v2)

    with pytest.raises(ProviderFallbackPolicyError, match="latest durable"):
        resolve_readonly_provider_route(
            registry=registry,
            required_capability=BookmakerCapability.LIVE_QUOTES_READ,
            primary_route=_route(v1),
            primary_integration=_integration(v1),
            primary_operational=_operational(
                v1, state=ProviderOperationalState.HEALTHY
            ),
            decided_at=T3,
        )


def test_stale_fallback_profile_fails_closed_after_registry_advances(tmp_path):
    primary = _profile(venue="primary", account="acct-p", adapter="api-p")
    f1 = _profile(
        venue="fallback", account="acct-f", adapter="api-f", version=1, source_sha=SHA_D
    )
    registry = _registry(tmp_path, primary, f1)
    f2 = _profile(
        venue="fallback",
        account="acct-f",
        adapter="api-f",
        version=2,
        observed_at=T1,
        source_sha=SHA_C,
    )
    assert registry.register_profile(f2)

    with pytest.raises(ProviderFallbackPolicyError, match="latest durable"):
        _resolve(
            registry,
            primary,
            primary_state=ProviderOperationalState.UNAVAILABLE,
            fallback=f1,
        )


def test_invented_profile_id_cannot_substitute_for_registry_truth(tmp_path):
    primary = _profile(venue="primary", account="acct-p", adapter="api-p")
    registry = _registry(tmp_path, primary)
    forged_route = replace(_route(primary), profile_id=SHA_D)
    forged_operational = replace(
        _operational(primary, state=ProviderOperationalState.HEALTHY),
        route=forged_route,
    )

    with pytest.raises(ProviderFallbackPolicyError, match="latest durable"):
        resolve_readonly_provider_route(
            registry=registry,
            required_capability=BookmakerCapability.LIVE_QUOTES_READ,
            primary_route=forged_route,
            primary_integration=_integration(primary),
            primary_operational=forged_operational,
            decided_at=T3,
        )


def test_integration_evidence_for_another_profile_fails_closed(tmp_path):
    primary = _profile(venue="primary", account="acct-p", adapter="api-p")
    other = _profile(venue="other", account="acct-o", adapter="api-o", source_sha=SHA_D)
    registry = _registry(tmp_path, primary, other)

    with pytest.raises(ProviderFallbackPolicyError, match="integration/profile"):
        resolve_readonly_provider_route(
            registry=registry,
            required_capability=BookmakerCapability.LIVE_QUOTES_READ,
            primary_route=_route(primary),
            primary_integration=_integration(other),
            primary_operational=_operational(
                primary, state=ProviderOperationalState.HEALTHY
            ),
            decided_at=T3,
        )


def test_operational_evidence_for_another_route_fails_closed(tmp_path):
    primary = _profile(venue="primary", account="acct-p", adapter="api-p")
    other = _profile(venue="other", account="acct-o", adapter="api-o", source_sha=SHA_D)
    registry = _registry(tmp_path, primary, other)

    with pytest.raises(ProviderFallbackPolicyError, match="another route"):
        resolve_readonly_provider_route(
            registry=registry,
            required_capability=BookmakerCapability.LIVE_QUOTES_READ,
            primary_route=_route(primary),
            primary_integration=_integration(primary),
            primary_operational=_operational(
                other, state=ProviderOperationalState.HEALTHY
            ),
            decided_at=T3,
        )


def test_operational_evidence_cannot_predate_profile(tmp_path):
    primary = _profile(
        venue="primary", account="acct-p", adapter="api-p", observed_at=T1
    )
    registry = _registry(tmp_path, primary)

    with pytest.raises(ProviderFallbackPolicyError, match="predates"):
        resolve_readonly_provider_route(
            registry=registry,
            required_capability=BookmakerCapability.LIVE_QUOTES_READ,
            primary_route=_route(primary),
            primary_integration=_integration(primary, observed_at=T1),
            primary_operational=_operational(
                primary,
                state=ProviderOperationalState.HEALTHY,
                observed_at=T0,
            ),
            decided_at=T3,
        )


@pytest.mark.parametrize("future_surface", ["integration", "operational"])
def test_future_route_evidence_is_rejected(tmp_path, future_surface):
    primary = _profile(venue="primary", account="acct-p", adapter="api-p")
    registry = _registry(tmp_path, primary)
    integration = _integration(
        primary, observed_at=T4 if future_surface == "integration" else T1
    )
    operational = _operational(
        primary,
        state=ProviderOperationalState.HEALTHY,
        observed_at=T4 if future_surface == "operational" else T2,
    )

    with pytest.raises(ProviderFallbackPolicyError, match="future at decided_at"):
        resolve_readonly_provider_route(
            registry=registry,
            required_capability=BookmakerCapability.LIVE_QUOTES_READ,
            primary_route=_route(primary),
            primary_integration=integration,
            primary_operational=operational,
            decided_at=T3,
        )


def test_partial_fallback_evidence_is_rejected(tmp_path):
    primary = _profile(venue="primary", account="acct-p", adapter="api-p")
    fallback = _profile(
        venue="fallback", account="acct-f", adapter="api-f", source_sha=SHA_D
    )
    registry = _registry(tmp_path, primary, fallback)

    with pytest.raises(ProviderFallbackPolicyError, match="supplied together"):
        resolve_readonly_provider_route(
            registry=registry,
            required_capability=BookmakerCapability.LIVE_QUOTES_READ,
            primary_route=_route(primary),
            primary_integration=_integration(primary),
            primary_operational=_operational(
                primary, state=ProviderOperationalState.UNAVAILABLE
            ),
            fallback_route=_route(fallback),
            fallback_integration=None,
            fallback_operational=None,
            decided_at=T3,
        )


def test_primary_profile_cannot_be_its_own_fallback(tmp_path):
    primary = _profile(venue="primary", account="acct-p", adapter="api-p")
    with pytest.raises(ProviderFallbackPolicyError, match="must not reuse"):
        _resolve(
            _registry(tmp_path, primary),
            primary,
            primary_state=ProviderOperationalState.UNAVAILABLE,
            fallback=primary,
        )


def test_decision_identity_is_deterministic_and_binds_operational_state(tmp_path):
    primary = _profile(venue="primary", account="acct-p", adapter="api-p")
    fallback = _profile(
        venue="fallback", account="acct-f", adapter="api-f", source_sha=SHA_D
    )
    registry = _registry(tmp_path, primary, fallback)
    first = _resolve(
        registry,
        primary,
        primary_state=ProviderOperationalState.UNAVAILABLE,
        fallback=fallback,
    )
    same = _resolve(
        registry,
        primary,
        primary_state=ProviderOperationalState.UNAVAILABLE,
        fallback=fallback,
    )
    different = _resolve(
        registry,
        primary,
        primary_state=ProviderOperationalState.DEGRADED,
        fallback=fallback,
    )

    assert first.decision_id == same.decision_id
    assert first.decision_id != different.decision_id


def test_operational_observation_identity_binds_source_evidence():
    profile = _profile(venue="primary", account="acct-p", adapter="api-p")
    observation = _operational(profile, state=ProviderOperationalState.HEALTHY)
    changed = replace(observation, source_payload_sha256=SHA_D)

    assert observation.observation_id != changed.observation_id


def test_corrupt_registry_fails_closed_instead_of_accepting_caller_profile(tmp_path):
    primary = _profile(venue="primary", account="acct-p", adapter="api-p")
    path = tmp_path / "bookmaker_capabilities.json"
    path.write_text('{"schema_version":1,"profiles":[', encoding="utf-8")
    registry = BookmakerCapabilityRegistry(path)

    with pytest.raises(ProviderFallbackPolicyError, match="cannot resolve canonical"):
        resolve_readonly_provider_route(
            registry=registry,
            required_capability=BookmakerCapability.LIVE_QUOTES_READ,
            primary_route=_route(primary),
            primary_integration=_integration(primary),
            primary_operational=_operational(
                primary, state=ProviderOperationalState.HEALTHY
            ),
            decided_at=T3,
        )

def test_account_scoped_read_cannot_fallback_to_another_account(tmp_path):
    primary = _profile(
        venue="venue-a",
        account="acct-a",
        adapter="api-a",
        capability=BookmakerCapability.BALANCE_READ,
    )
    fallback = _profile(
        venue="venue-b",
        account="acct-b",
        adapter="api-b",
        capability=BookmakerCapability.BALANCE_READ,
        source_sha=SHA_D,
    )
    decision = _resolve(
        _registry(tmp_path, primary, fallback),
        primary,
        primary_state=ProviderOperationalState.UNAVAILABLE,
        fallback=fallback,
        capability=BookmakerCapability.BALANCE_READ,
    )

    assert decision.disposition is ProviderFallbackDisposition.ABSTAIN
    assert decision.reason == "ACCOUNT_SCOPED_FALLBACK_IDENTITY_MISMATCH"


def test_account_scoped_read_can_use_alternate_adapter_for_same_account(tmp_path):
    primary = _profile(
        venue="venue-a",
        account="acct-a",
        adapter="api-primary",
        capability=BookmakerCapability.BALANCE_READ,
    )
    fallback = _profile(
        venue="venue-a",
        account="acct-a",
        adapter="api-secondary",
        capability=BookmakerCapability.BALANCE_READ,
        source_sha=SHA_D,
    )
    decision = _resolve(
        _registry(tmp_path, primary, fallback),
        primary,
        primary_state=ProviderOperationalState.UNAVAILABLE,
        fallback=fallback,
        capability=BookmakerCapability.BALANCE_READ,
    )

    assert decision.disposition is ProviderFallbackDisposition.USE_FALLBACK_READ
    assert decision.selected_profile_id == fallback.profile_id
    assert decision.downstream_semantic_compatibility_required is False


def test_cross_provider_quote_fallback_requires_semantic_compatibility_gate(tmp_path):
    primary = _profile(venue="venue-a", account="acct-a", adapter="api-a")
    fallback = _profile(
        venue="venue-b", account="acct-b", adapter="api-b", source_sha=SHA_D
    )
    decision = _resolve(
        _registry(tmp_path, primary, fallback),
        primary,
        primary_state=ProviderOperationalState.UNAVAILABLE,
        fallback=fallback,
    )

    assert decision.disposition is ProviderFallbackDisposition.USE_FALLBACK_READ
    assert decision.downstream_semantic_compatibility_required is True
    assert decision.downstream_source_quality_required is True


def test_partial_fallback_is_rejected_even_when_primary_is_healthy(tmp_path):
    primary = _profile(venue="primary", account="acct-p", adapter="api-p")
    fallback = _profile(
        venue="fallback", account="acct-f", adapter="api-f", source_sha=SHA_D
    )
    registry = _registry(tmp_path, primary, fallback)

    with pytest.raises(ProviderFallbackPolicyError, match="supplied together"):
        resolve_readonly_provider_route(
            registry=registry,
            required_capability=BookmakerCapability.LIVE_QUOTES_READ,
            primary_route=_route(primary),
            primary_integration=_integration(primary),
            primary_operational=_operational(
                primary, state=ProviderOperationalState.HEALTHY
            ),
            fallback_route=_route(fallback),
            fallback_integration=None,
            fallback_operational=None,
            decided_at=T3,
        )

