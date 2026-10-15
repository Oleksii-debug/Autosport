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
from autosport.ingestion_health import IngestionPolicy, SourceHealthStore
from autosport.provider_fallback_policy import (
    ProviderFallbackDisposition,
    ProviderFallbackPolicyError,
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
T5 = "2026-09-21T10:05:00+00:00"


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


def _route(
    profile: BookmakerCapabilityProfile,
    *,
    source_id: str | None = None,
) -> ProviderRouteIdentity:
    return ProviderRouteIdentity(
        source_id=source_id or f"source:{profile.venue_id}:{profile.adapter_id}",
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


def _registry(
    tmp_path,
    *profiles: BookmakerCapabilityProfile,
) -> BookmakerCapabilityRegistry:
    registry = BookmakerCapabilityRegistry(tmp_path / "bookmaker_capabilities.json")
    for profile in profiles:
        assert registry.register_profile(profile)
    return registry


def _health_store(tmp_path) -> SourceHealthStore:
    return SourceHealthStore(tmp_path / "source_health.json")


def _record_health(
    store: SourceHealthStore,
    source_id: str,
    state: ProviderOperationalState,
    *,
    at: str = T2,
) -> None:
    if state is ProviderOperationalState.UNKNOWN:
        return
    if state is ProviderOperationalState.HEALTHY:
        store.record_success(
            source_id,
            now=at,
            received=1,
            accepted=1,
            rejected=0,
            cursor="1",
            latest_source_ts=at,
            quality_flags=(),
        )
        return
    if state is ProviderOperationalState.DEGRADED:
        store.record_success(
            source_id,
            now=at,
            received=1,
            accepted=1,
            rejected=0,
            cursor="1",
            latest_source_ts=at,
            quality_flags=("DEGRADED_TEST_EVIDENCE",),
        )
        return
    if state is ProviderOperationalState.UNAVAILABLE:
        store.record_failure(source_id, now=at, error=RuntimeError("provider unavailable"))
        return
    if state is ProviderOperationalState.STALE:
        store.record_success(
            source_id,
            now=T0,
            received=1,
            accepted=1,
            rejected=0,
            cursor="1",
            latest_source_ts=T0,
            quality_flags=(),
        )
        return
    raise AssertionError(f"unsupported fixture state {state}")


def _resolve(
    tmp_path,
    primary: BookmakerCapabilityProfile,
    *,
    primary_state: ProviderOperationalState,
    fallback: BookmakerCapabilityProfile | None = None,
    fallback_state: ProviderOperationalState = ProviderOperationalState.HEALTHY,
    capability: BookmakerCapability = BookmakerCapability.LIVE_QUOTES_READ,
    fallback_kind: BookmakerIntegrationKind = BookmakerIntegrationKind.OFFICIAL_API,
    policy: IngestionPolicy | None = None,
):
    profiles = (primary,) if fallback is None else (primary, fallback)
    registry = _registry(tmp_path, *profiles)
    health_store = _health_store(tmp_path)
    primary_route = _route(primary)
    _record_health(health_store, primary_route.source_id, primary_state)
    fallback_args = {}
    if fallback is not None:
        fallback_route = _route(fallback)
        _record_health(health_store, fallback_route.source_id, fallback_state)
        fallback_args = {
            "fallback_route": fallback_route,
            "fallback_integration": _integration(fallback, kind=fallback_kind),
        }
    return resolve_readonly_provider_route(
        registry=registry,
        health_store=health_store,
        required_capability=capability,
        primary_route=primary_route,
        primary_integration=_integration(primary),
        decided_at=T3,
        policy=policy,
        **fallback_args,
    )


def test_healthy_supported_primary_is_only_technically_eligible(tmp_path):
    primary = _profile(venue="primary", account="acct-p", adapter="api-p")
    decision = _resolve(
        tmp_path,
        primary,
        primary_state=ProviderOperationalState.HEALTHY,
    )

    assert (
        decision.disposition
        is ProviderFallbackDisposition.PRIMARY_TECHNICALLY_ELIGIBLE
    )
    assert decision.selected_profile_id == primary.profile_id
    assert decision.primary_health_state is ProviderOperationalState.HEALTHY
    assert decision.source_quality_authority is False
    assert decision.source_identity_authority is False
    assert decision.execution_authority is False
    assert decision.downstream_source_quality_required is True
    assert decision.downstream_source_identity_binding_required is True


@pytest.mark.parametrize(
    "primary_state",
    [
        ProviderOperationalState.DEGRADED,
        ProviderOperationalState.UNAVAILABLE,
        ProviderOperationalState.STALE,
        ProviderOperationalState.UNKNOWN,
    ],
)
def test_nonhealthy_primary_can_yield_healthy_supported_fallback_candidate(
    tmp_path, primary_state
):
    primary = _profile(venue="primary", account="acct-p", adapter="api-p")
    fallback = _profile(
        venue="fallback",
        account="acct-f",
        adapter="api-f",
        source_sha=SHA_D,
    )
    decision = _resolve(
        tmp_path,
        primary,
        primary_state=primary_state,
        fallback=fallback,
    )

    assert (
        decision.disposition
        is ProviderFallbackDisposition.FALLBACK_TECHNICALLY_ELIGIBLE
    )
    assert decision.selected_profile_id == fallback.profile_id
    assert decision.primary_health_state is primary_state
    assert decision.fallback_health_state is ProviderOperationalState.HEALTHY
    assert (
        decision.reason
        == f"PRIMARY_{primary_state.value}_FALLBACK_HEALTHY_SUPPORTED"
    )
    assert decision.downstream_source_quality_required is True
    assert decision.downstream_source_identity_binding_required is True
    assert decision.downstream_semantic_compatibility_required is True


@pytest.mark.parametrize(
    "fallback_state",
    [
        ProviderOperationalState.DEGRADED,
        ProviderOperationalState.UNAVAILABLE,
        ProviderOperationalState.STALE,
        ProviderOperationalState.UNKNOWN,
    ],
)
def test_nonhealthy_fallback_never_becomes_candidate(tmp_path, fallback_state):
    primary = _profile(venue="primary", account="acct-p", adapter="api-p")
    fallback = _profile(
        venue="fallback",
        account="acct-f",
        adapter="api-f",
        source_sha=SHA_D,
    )
    decision = _resolve(
        tmp_path,
        primary,
        primary_state=ProviderOperationalState.UNAVAILABLE,
        fallback=fallback,
        fallback_state=fallback_state,
    )

    assert decision.disposition is ProviderFallbackDisposition.ABSTAIN
    assert decision.reason == f"FALLBACK_{fallback_state.value}"
    assert decision.downstream_source_quality_required is False
    assert decision.downstream_source_identity_binding_required is False


@pytest.mark.parametrize(
    "capability_state, expected_reason",
    [
        (
            BookmakerCapabilityState.UNSUPPORTED,
            "PRIMARY_HEALTHY_CAPABILITY_UNSUPPORTED",
        ),
        (BookmakerCapabilityState.UNKNOWN, "PRIMARY_HEALTHY_CAPABILITY_UNKNOWN"),
    ],
)
def test_healthy_primary_capability_gap_does_not_switch_sources(
    tmp_path, capability_state, expected_reason
):
    if capability_state is BookmakerCapabilityState.UNKNOWN:
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
            state=capability_state,
        )
    fallback = _profile(
        venue="fallback",
        account="acct-f",
        adapter="api-f",
        source_sha=SHA_D,
    )
    decision = _resolve(
        tmp_path,
        primary,
        primary_state=ProviderOperationalState.HEALTHY,
        fallback=fallback,
    )

    assert decision.disposition is ProviderFallbackDisposition.ABSTAIN
    assert decision.reason == expected_reason


def test_nonhealthy_primary_without_fallback_abstains(tmp_path):
    primary = _profile(venue="primary", account="acct-p", adapter="api-p")
    decision = _resolve(
        tmp_path,
        primary,
        primary_state=ProviderOperationalState.DEGRADED,
    )

    assert decision.disposition is ProviderFallbackDisposition.ABSTAIN
    assert decision.reason == "PRIMARY_DEGRADED_NO_FALLBACK"


@pytest.mark.parametrize(
    "fallback_capability_state, expected_reason",
    [
        (
            BookmakerCapabilityState.UNSUPPORTED,
            "FALLBACK_CAPABILITY_UNSUPPORTED",
        ),
        (BookmakerCapabilityState.UNKNOWN, "FALLBACK_CAPABILITY_UNKNOWN"),
    ],
)
def test_fallback_must_durably_support_exact_capability(
    tmp_path, fallback_capability_state, expected_reason
):
    primary = _profile(venue="primary", account="acct-p", adapter="api-p")
    if fallback_capability_state is BookmakerCapabilityState.UNKNOWN:
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
            state=fallback_capability_state,
            source_sha=SHA_D,
        )
    decision = _resolve(
        tmp_path,
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
def test_execution_or_execution_adjacent_capabilities_are_rejected(
    tmp_path, capability
):
    primary = _profile(venue="primary", account="acct-p", adapter="api-p")
    with pytest.raises(ProviderFallbackPolicyError, match="read-only"):
        _resolve(
            tmp_path,
            primary,
            primary_state=ProviderOperationalState.HEALTHY,
            capability=capability,
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
        tmp_path,
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
        tmp_path,
        primary,
        primary_state=ProviderOperationalState.UNAVAILABLE,
        fallback=fallback,
        capability=BookmakerCapability.BALANCE_READ,
    )

    assert (
        decision.disposition
        is ProviderFallbackDisposition.FALLBACK_TECHNICALLY_ELIGIBLE
    )
    assert decision.downstream_semantic_compatibility_required is False
    assert decision.downstream_source_identity_binding_required is True


def test_browser_fallback_is_not_promoted_to_source_quality_authority(tmp_path):
    primary = _profile(venue="primary", account="acct-p", adapter="api-p")
    fallback = _profile(
        venue="fallback",
        account="acct-f",
        adapter="browser-f",
        source_sha=SHA_D,
    )
    decision = _resolve(
        tmp_path,
        primary,
        primary_state=ProviderOperationalState.UNAVAILABLE,
        fallback=fallback,
        fallback_kind=BookmakerIntegrationKind.BROWSER_AUTOMATION,
    )

    assert (
        decision.disposition
        is ProviderFallbackDisposition.FALLBACK_TECHNICALLY_ELIGIBLE
    )
    assert decision.source_quality_authority is False
    assert decision.downstream_source_quality_required is True


def test_health_is_resolved_as_of_decision_without_future_leakage(tmp_path):
    primary = _profile(venue="primary", account="acct-p", adapter="api-p")
    registry = _registry(tmp_path, primary)
    health = _health_store(tmp_path)
    route = _route(primary)
    _record_health(health, route.source_id, ProviderOperationalState.HEALTHY, at=T2)
    health.record_failure(
        route.source_id,
        now=T4,
        error=RuntimeError("future failure"),
    )

    decision = resolve_readonly_provider_route(
        registry=registry,
        health_store=health,
        required_capability=BookmakerCapability.LIVE_QUOTES_READ,
        primary_route=route,
        primary_integration=_integration(primary),
        decided_at=T3,
    )

    assert (
        decision.disposition
        is ProviderFallbackDisposition.PRIMARY_TECHNICALLY_ELIGIBLE
    )
    assert decision.primary_health_state is ProviderOperationalState.HEALTHY


def test_health_freshness_uses_ingestion_policy_and_fails_closed(tmp_path):
    primary = _profile(venue="primary", account="acct-p", adapter="api-p")
    registry = _registry(tmp_path, primary)
    health = _health_store(tmp_path)
    route = _route(primary)
    _record_health(health, route.source_id, ProviderOperationalState.HEALTHY, at=T2)

    fresh = resolve_readonly_provider_route(
        registry=registry,
        health_store=health,
        required_capability=BookmakerCapability.LIVE_QUOTES_READ,
        primary_route=route,
        primary_integration=_integration(primary),
        decided_at=T3,
        policy=IngestionPolicy(stale_after_seconds=120.0),
    )
    stale = resolve_readonly_provider_route(
        registry=registry,
        health_store=health,
        required_capability=BookmakerCapability.LIVE_QUOTES_READ,
        primary_route=route,
        primary_integration=_integration(primary),
        decided_at=T3,
        policy=IngestionPolicy(stale_after_seconds=30.0),
    )

    assert (
        fresh.disposition
        is ProviderFallbackDisposition.PRIMARY_TECHNICALLY_ELIGIBLE
    )
    assert stale.disposition is ProviderFallbackDisposition.ABSTAIN
    assert stale.primary_health_state is ProviderOperationalState.STALE
    assert stale.reason == "PRIMARY_STALE_NO_FALLBACK"


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
    health = _health_store(tmp_path)
    route = _route(v1)
    _record_health(health, route.source_id, ProviderOperationalState.HEALTHY)

    with pytest.raises(ProviderFallbackPolicyError, match="latest durable"):
        resolve_readonly_provider_route(
            registry=registry,
            health_store=health,
            required_capability=BookmakerCapability.LIVE_QUOTES_READ,
            primary_route=route,
            primary_integration=_integration(v1),
            decided_at=T3,
        )


def test_invented_profile_id_cannot_substitute_for_registry_truth(tmp_path):
    primary = _profile(venue="primary", account="acct-p", adapter="api-p")
    registry = _registry(tmp_path, primary)
    route = replace(_route(primary), profile_id=SHA_D)
    health = _health_store(tmp_path)
    _record_health(health, route.source_id, ProviderOperationalState.HEALTHY)

    with pytest.raises(ProviderFallbackPolicyError, match="latest durable"):
        resolve_readonly_provider_route(
            registry=registry,
            health_store=health,
            required_capability=BookmakerCapability.LIVE_QUOTES_READ,
            primary_route=route,
            primary_integration=_integration(primary),
            decided_at=T3,
        )


def test_integration_evidence_for_another_profile_fails_closed(tmp_path):
    primary = _profile(venue="primary", account="acct-p", adapter="api-p")
    other = _profile(
        venue="other", account="acct-o", adapter="api-o", source_sha=SHA_D
    )
    registry = _registry(tmp_path, primary, other)
    health = _health_store(tmp_path)
    route = _route(primary)
    _record_health(health, route.source_id, ProviderOperationalState.HEALTHY)

    with pytest.raises(ProviderFallbackPolicyError, match="integration/profile"):
        resolve_readonly_provider_route(
            registry=registry,
            health_store=health,
            required_capability=BookmakerCapability.LIVE_QUOTES_READ,
            primary_route=route,
            primary_integration=_integration(other),
            decided_at=T3,
        )


@pytest.mark.parametrize("future_surface", ["profile", "integration"])
def test_future_technical_evidence_is_rejected(tmp_path, future_surface):
    profile_at = T4 if future_surface == "profile" else T0
    primary = _profile(
        venue="primary",
        account="acct-p",
        adapter="api-p",
        observed_at=profile_at,
    )
    registry = _registry(tmp_path, primary)
    health = _health_store(tmp_path)
    route = _route(primary)
    _record_health(health, route.source_id, ProviderOperationalState.HEALTHY)
    integration_at = T4 if future_surface == "integration" else T4

    with pytest.raises(ProviderFallbackPolicyError, match="future at decided_at"):
        resolve_readonly_provider_route(
            registry=registry,
            health_store=health,
            required_capability=BookmakerCapability.LIVE_QUOTES_READ,
            primary_route=route,
            primary_integration=_integration(
                primary,
                observed_at=integration_at,
            ),
            decided_at=T3,
        )


def test_partial_fallback_evidence_is_rejected_even_when_primary_healthy(tmp_path):
    primary = _profile(venue="primary", account="acct-p", adapter="api-p")
    fallback = _profile(
        venue="fallback", account="acct-f", adapter="api-f", source_sha=SHA_D
    )
    registry = _registry(tmp_path, primary, fallback)
    health = _health_store(tmp_path)
    route = _route(primary)
    _record_health(health, route.source_id, ProviderOperationalState.HEALTHY)

    with pytest.raises(ProviderFallbackPolicyError, match="supplied together"):
        resolve_readonly_provider_route(
            registry=registry,
            health_store=health,
            required_capability=BookmakerCapability.LIVE_QUOTES_READ,
            primary_route=route,
            primary_integration=_integration(primary),
            fallback_route=_route(fallback),
            fallback_integration=None,
            decided_at=T3,
        )


def test_exact_primary_source_and_profile_cannot_be_its_own_fallback(tmp_path):
    primary = _profile(venue="primary", account="acct-p", adapter="api-p")
    registry = _registry(tmp_path, primary)
    health = _health_store(tmp_path)
    route = _route(primary)
    _record_health(health, route.source_id, ProviderOperationalState.UNAVAILABLE)

    with pytest.raises(ProviderFallbackPolicyError, match="must not reuse"):
        resolve_readonly_provider_route(
            registry=registry,
            health_store=health,
            required_capability=BookmakerCapability.LIVE_QUOTES_READ,
            primary_route=route,
            primary_integration=_integration(primary),
            fallback_route=route,
            fallback_integration=_integration(primary),
            decided_at=T3,
        )


def test_decision_identity_is_deterministic_for_same_durable_truth(tmp_path):
    primary = _profile(venue="primary", account="acct-p", adapter="api-p")
    fallback = _profile(
        venue="fallback", account="acct-f", adapter="api-f", source_sha=SHA_D
    )
    registry = _registry(tmp_path, primary, fallback)
    health = _health_store(tmp_path)
    primary_route = _route(primary)
    fallback_route = _route(fallback)
    _record_health(
        health,
        primary_route.source_id,
        ProviderOperationalState.UNAVAILABLE,
    )
    _record_health(
        health,
        fallback_route.source_id,
        ProviderOperationalState.HEALTHY,
    )
    kwargs = dict(
        registry=registry,
        health_store=health,
        required_capability=BookmakerCapability.LIVE_QUOTES_READ,
        primary_route=primary_route,
        primary_integration=_integration(primary),
        fallback_route=fallback_route,
        fallback_integration=_integration(fallback),
        decided_at=T3,
    )

    first = resolve_readonly_provider_route(**kwargs)
    second = resolve_readonly_provider_route(**kwargs)

    assert first.decision_id == second.decision_id
    assert first.primary_health_snapshot_id == second.primary_health_snapshot_id
    assert first.fallback_health_snapshot_id == second.fallback_health_snapshot_id


def test_new_durable_health_transition_changes_decision_identity(tmp_path):
    primary = _profile(venue="primary", account="acct-p", adapter="api-p")
    registry = _registry(tmp_path, primary)
    health = _health_store(tmp_path)
    route = _route(primary)
    _record_health(health, route.source_id, ProviderOperationalState.HEALTHY, at=T2)

    before = resolve_readonly_provider_route(
        registry=registry,
        health_store=health,
        required_capability=BookmakerCapability.LIVE_QUOTES_READ,
        primary_route=route,
        primary_integration=_integration(primary),
        decided_at=T3,
    )
    health.record_failure(route.source_id, now=T4, error=RuntimeError("lost"))
    after = resolve_readonly_provider_route(
        registry=registry,
        health_store=health,
        required_capability=BookmakerCapability.LIVE_QUOTES_READ,
        primary_route=route,
        primary_integration=_integration(primary),
        decided_at=T5,
    )

    assert before.decision_id != after.decision_id
    assert before.primary_health_state is ProviderOperationalState.HEALTHY
    assert after.primary_health_state is ProviderOperationalState.UNAVAILABLE
    assert after.disposition is ProviderFallbackDisposition.ABSTAIN


def test_corrupt_registry_fails_closed(tmp_path):
    primary = _profile(venue="primary", account="acct-p", adapter="api-p")
    registry_path = tmp_path / "bookmaker_capabilities.json"
    registry_path.write_text('{"schema_version":1,"profiles":[', encoding="utf-8")
    registry = BookmakerCapabilityRegistry.__new__(BookmakerCapabilityRegistry)
    registry.path = registry_path
    health = _health_store(tmp_path)
    route = _route(primary)
    _record_health(health, route.source_id, ProviderOperationalState.HEALTHY)

    with pytest.raises(ProviderFallbackPolicyError, match="cannot resolve canonical"):
        resolve_readonly_provider_route(
            registry=registry,
            health_store=health,
            required_capability=BookmakerCapability.LIVE_QUOTES_READ,
            primary_route=route,
            primary_integration=_integration(primary),
            decided_at=T3,
        )


def test_corrupt_health_store_fails_closed(tmp_path):
    primary = _profile(venue="primary", account="acct-p", adapter="api-p")
    registry = _registry(tmp_path, primary)
    health = _health_store(tmp_path)
    route = _route(primary)
    health.path.write_text('{"schema_version":3,"sources":', encoding="utf-8")

    with pytest.raises(ProviderFallbackPolicyError, match="durable source health"):
        resolve_readonly_provider_route(
            registry=registry,
            health_store=health,
            required_capability=BookmakerCapability.LIVE_QUOTES_READ,
            primary_route=route,
            primary_integration=_integration(primary),
            decided_at=T3,
        )


def test_abstention_grants_no_downstream_gates(tmp_path):
    primary = _profile(
        venue="primary",
        account="acct-p",
        adapter="api-p",
        state=BookmakerCapabilityState.UNSUPPORTED,
    )
    decision = _resolve(
        tmp_path,
        primary,
        primary_state=ProviderOperationalState.HEALTHY,
    )

    assert decision.disposition is ProviderFallbackDisposition.ABSTAIN
    assert decision.downstream_source_quality_required is False
    assert decision.downstream_source_identity_binding_required is False
    assert decision.downstream_semantic_compatibility_required is False
    assert decision.execution_authority is False
    assert decision.source_quality_authority is False
    assert decision.source_identity_authority is False
