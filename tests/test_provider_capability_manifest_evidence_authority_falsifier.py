import pytest

import autosport.provider_capability_manifest as provider_manifest_module

from autosport.bookmaker_capability import (
    BookmakerCapability,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
)
from autosport.bookmaker_integration_boundary import (
    BookmakerIntegrationKind,
    bind_bookmaker_integration,
)
from autosport.provider_capability_manifest import (
    ProviderCapabilityEvidenceRef,
    ProviderCapabilityManifestError,
    ProviderCapabilityManifestFact,
    ProviderManifestCapability,
    ProviderManifestFactAuthority,
    ProviderManifestState,
    build_provider_capability_manifest,
)


_T0 = "2026-09-21T13:00:00+00:00"
_T1 = "2026-09-21T13:01:00+00:00"
_T_EVIDENCE = "2026-09-21T13:01:30+00:00"
_T2 = "2026-09-21T13:02:00+00:00"
_HASH_A = "a" * 64
_HASH_B = "b" * 64
_HASH_C = "c" * 64


def _profile() -> BookmakerCapabilityProfile:
    return BookmakerCapabilityProfile(
        venue_id="betfair",
        account_id="acct-a",
        adapter_id="betfair-api",
        adapter_version="1.0",
        profile_version=3,
        facts=(
            BookmakerCapabilityFact(
                BookmakerCapability.LIVE_QUOTES_READ,
                BookmakerCapabilityState.SUPPORTED,
            ),
            BookmakerCapabilityFact(
                BookmakerCapability.SETTLED_POSITIONS_READ,
                BookmakerCapabilityState.SUPPORTED,
            ),
            BookmakerCapabilityFact(
                BookmakerCapability.PLACE_BET,
                BookmakerCapabilityState.SUPPORTED,
            ),
        ),
        observed_at=_T0,
        source_ref="capability-profile",
        source_payload_sha256=_HASH_A,
    )


def test_caller_authored_extension_evidence_cannot_mint_proven_capability_truth() -> None:
    profile = _profile()
    integration = bind_bookmaker_integration(
        profile,
        integration_kind=BookmakerIntegrationKind.OFFICIAL_API,
        observed_at=_T1,
        source_ref="integration-manifest",
        source_payload_sha256=_HASH_B,
    )

    caller_authored = ProviderCapabilityEvidenceRef(
        kind="caller-self-asserted-stream-proof",
        evidence_ref="caller://self-authored-provider-proof",
        evidence_sha256=_HASH_C,
        observed_at=_T_EVIDENCE,
        profile_id=profile.profile_id,
        integration_evidence_id=integration.evidence_id,
    )
    forged_stream_fact = ProviderCapabilityManifestFact(
        capability=ProviderManifestCapability.STREAM,
        state=ProviderManifestState.PROVEN,
        authority=ProviderManifestFactAuthority.EXPLICIT_EVIDENCE,
        evidence=caller_authored,
    )

    # Binding a self-authored reference to genuine profile/integration IDs proves
    # identity compatibility, not provider-origin or product-owned evidence
    # authority. Positive extension capability truth must therefore fail closed
    # until the reference is resolved through a product-owned/re-verifiable
    # evidence authority rather than trusted by construction alone.
    with pytest.raises(ProviderCapabilityManifestError):
        build_provider_capability_manifest(
            profile,
            integration,
            manifest_ref="provider-capability-manifest",
            manifest_version=7,
            observed_at=_T2,
            source_ref="product-provider-capability-projection",
            source_payload_sha256=_HASH_C,
            extension_facts=(forged_stream_fact,),
        )

def test_canonical_capability_authority_cannot_be_mutated_or_rebound() -> None:
    profile = _profile()
    integration = bind_bookmaker_integration(
        profile,
        integration_kind=BookmakerIntegrationKind.OFFICIAL_API,
        observed_at=_T1,
        source_ref="integration-manifest",
        source_payload_sha256=_HASH_B,
    )

    canonical_map = provider_manifest_module._CANONICAL_CAPABILITY_MAP
    with pytest.raises(TypeError):
        canonical_map[ProviderManifestCapability.STREAM] = (
            BookmakerCapability.LIVE_QUOTES_READ
        )

    original_global = provider_manifest_module._CANONICAL_CAPABILITY_MAP
    try:
        provider_manifest_module._CANONICAL_CAPABILITY_MAP = {
            ProviderManifestCapability.STREAM: BookmakerCapability.LIVE_QUOTES_READ,
        }
        manifest = build_provider_capability_manifest(
            profile,
            integration,
            manifest_ref="provider-capability-manifest",
            manifest_version=8,
            observed_at=_T2,
            source_ref="product-provider-capability-projection",
            source_payload_sha256=_HASH_C,
        )
    finally:
        provider_manifest_module._CANONICAL_CAPABILITY_MAP = original_global

    assert manifest.state_of(ProviderManifestCapability.LIVE_QUOTES) is (
        ProviderManifestState.PROVEN
    )
    assert manifest.state_of(ProviderManifestCapability.STREAM) is (
        ProviderManifestState.NOT_PROVEN
    )

def test_authority_dispatch_rebinding_cannot_mint_capability_truth() -> None:
    profile = _profile()
    integration = bind_bookmaker_integration(
        profile,
        integration_kind=BookmakerIntegrationKind.OFFICIAL_API,
        observed_at=_T1,
        source_ref="integration-manifest",
        source_payload_sha256=_HASH_B,
    )

    original_resolver = provider_manifest_module._canonical_capability_for
    original_profile_state = provider_manifest_module._profile_state
    try:
        provider_manifest_module._canonical_capability_for = (
            lambda capability: BookmakerCapability.LIVE_QUOTES_READ
        )
        provider_manifest_module._profile_state = (
            lambda profile, capability: ProviderManifestState.PROVEN
        )
        manifest = build_provider_capability_manifest(
            profile,
            integration,
            manifest_ref="provider-capability-manifest",
            manifest_version=9,
            observed_at=_T2,
            source_ref="product-provider-capability-projection",
            source_payload_sha256=_HASH_C,
        )
    finally:
        provider_manifest_module._canonical_capability_for = original_resolver
        provider_manifest_module._profile_state = original_profile_state

    assert manifest.state_of(ProviderManifestCapability.LIVE_QUOTES) is (
        ProviderManifestState.PROVEN
    )
    assert manifest.state_of(ProviderManifestCapability.PREMATCH_QUOTES) is (
        ProviderManifestState.NOT_PROVEN
    )
    assert manifest.state_of(ProviderManifestCapability.STREAM) is (
        ProviderManifestState.NOT_PROVEN
    )

def test_profile_state_method_rebinding_cannot_mint_canonical_capability_truth() -> None:
    profile = _profile()
    integration = bind_bookmaker_integration(
        profile,
        integration_kind=BookmakerIntegrationKind.OFFICIAL_API,
        observed_at=_T1,
        source_ref="integration-manifest",
        source_payload_sha256=_HASH_B,
    )

    original_state_of = BookmakerCapabilityProfile.state_of
    try:
        BookmakerCapabilityProfile.state_of = (
            lambda self, capability: BookmakerCapabilityState.SUPPORTED
        )
        manifest = build_provider_capability_manifest(
            profile,
            integration,
            manifest_ref="provider-capability-manifest",
            manifest_version=10,
            observed_at=_T2,
            source_ref="product-provider-capability-projection",
            source_payload_sha256=_HASH_C,
        )
    finally:
        BookmakerCapabilityProfile.state_of = original_state_of

    assert manifest.state_of(ProviderManifestCapability.LIVE_QUOTES) is (
        ProviderManifestState.PROVEN
    )
    assert manifest.state_of(ProviderManifestCapability.PREMATCH_QUOTES) is (
        ProviderManifestState.NOT_PROVEN
    )
    assert manifest.state_of(ProviderManifestCapability.STREAM) is (
        ProviderManifestState.NOT_PROVEN
    )

def test_postconstruction_extension_state_mutation_cannot_mint_truth() -> None:
    profile = _profile()
    integration = bind_bookmaker_integration(
        profile,
        integration_kind=BookmakerIntegrationKind.OFFICIAL_API,
        observed_at=_T1,
        source_ref="integration-manifest",
        source_payload_sha256=_HASH_B,
    )
    manifest = build_provider_capability_manifest(
        profile,
        integration,
        manifest_ref="provider-capability-manifest",
        manifest_version=11,
        observed_at=_T2,
        source_ref="product-provider-capability-projection",
        source_payload_sha256=_HASH_C,
    )
    stream = next(
        fact
        for fact in manifest.facts
        if fact.capability is ProviderManifestCapability.STREAM
    )
    object.__setattr__(stream, "state", ProviderManifestState.PROVEN)

    with pytest.raises(
        ProviderCapabilityManifestError,
        match="stream extension truth changed after validation",
    ):
        manifest.supports(ProviderManifestCapability.STREAM)


def test_postconstruction_extension_authority_mutation_cannot_mint_truth() -> None:
    profile = _profile()
    integration = bind_bookmaker_integration(
        profile,
        integration_kind=BookmakerIntegrationKind.OFFICIAL_API,
        observed_at=_T1,
        source_ref="integration-manifest",
        source_payload_sha256=_HASH_B,
    )
    manifest = build_provider_capability_manifest(
        profile,
        integration,
        manifest_ref="provider-capability-manifest",
        manifest_version=12,
        observed_at=_T2,
        source_ref="product-provider-capability-projection",
        source_payload_sha256=_HASH_C,
    )
    stream = next(
        fact
        for fact in manifest.facts
        if fact.capability is ProviderManifestCapability.STREAM
    )
    object.__setattr__(
        stream,
        "authority",
        ProviderManifestFactAuthority.EXPLICIT_EVIDENCE,
    )

    with pytest.raises(
        ProviderCapabilityManifestError,
        match="stream extension truth changed after validation",
    ):
        manifest.state_of(ProviderManifestCapability.STREAM)


def test_postconstruction_canonical_state_mutation_cannot_widen_truth() -> None:
    profile = _profile()
    integration = bind_bookmaker_integration(
        profile,
        integration_kind=BookmakerIntegrationKind.OFFICIAL_API,
        observed_at=_T1,
        source_ref="integration-manifest",
        source_payload_sha256=_HASH_B,
    )
    manifest = build_provider_capability_manifest(
        profile,
        integration,
        manifest_ref="provider-capability-manifest",
        manifest_version=13,
        observed_at=_T2,
        source_ref="product-provider-capability-projection",
        source_payload_sha256=_HASH_C,
    )
    prematch = next(
        fact
        for fact in manifest.facts
        if fact.capability is ProviderManifestCapability.PREMATCH_QUOTES
    )
    assert prematch.state is ProviderManifestState.NOT_PROVEN
    object.__setattr__(prematch, "state", ProviderManifestState.PROVEN)

    with pytest.raises(
        ProviderCapabilityManifestError,
        match="prematch_quotes canonical truth changed after validation",
    ):
        manifest.supports(ProviderManifestCapability.PREMATCH_QUOTES)


def test_postconstruction_fact_tuple_replacement_fails_closed() -> None:
    profile = _profile()
    integration = bind_bookmaker_integration(
        profile,
        integration_kind=BookmakerIntegrationKind.OFFICIAL_API,
        observed_at=_T1,
        source_ref="integration-manifest",
        source_payload_sha256=_HASH_B,
    )
    manifest = build_provider_capability_manifest(
        profile,
        integration,
        manifest_ref="provider-capability-manifest",
        manifest_version=14,
        observed_at=_T2,
        source_ref="product-provider-capability-projection",
        source_payload_sha256=_HASH_C,
    )
    object.__setattr__(manifest, "facts", manifest.facts[:-1])

    with pytest.raises(
        ProviderCapabilityManifestError,
        match="manifest fact vocabulary changed after validation",
    ):
        manifest.state_of(ProviderManifestCapability.LIVE_QUOTES)


def test_postconstruction_profile_mutation_invalidates_manifest_read() -> None:
    profile = _profile()
    integration = bind_bookmaker_integration(
        profile,
        integration_kind=BookmakerIntegrationKind.OFFICIAL_API,
        observed_at=_T1,
        source_ref="integration-manifest",
        source_payload_sha256=_HASH_B,
    )
    manifest = build_provider_capability_manifest(
        profile,
        integration,
        manifest_ref="provider-capability-manifest",
        manifest_version=15,
        observed_at=_T2,
        source_ref="product-provider-capability-projection",
        source_payload_sha256=_HASH_C,
    )
    live = next(
        fact
        for fact in profile.facts
        if fact.capability is BookmakerCapability.LIVE_QUOTES_READ
    )
    object.__setattr__(live, "state", BookmakerCapabilityState.UNSUPPORTED)

    with pytest.raises(Exception, match="does not match capability profile identity"):
        manifest.state_of(ProviderManifestCapability.LIVE_QUOTES)


def test_supports_rejects_state_reader_rebinding(monkeypatch) -> None:
    profile = _profile()
    integration = bind_bookmaker_integration(
        profile,
        integration_kind=BookmakerIntegrationKind.OFFICIAL_API,
        observed_at=_T1,
        source_ref="integration-manifest",
        source_payload_sha256=_HASH_B,
    )
    manifest = build_provider_capability_manifest(
        profile,
        integration,
        manifest_ref="provider-capability-manifest",
        manifest_version=16,
        observed_at=_T2,
        source_ref="product-provider-capability-projection",
        source_payload_sha256=_HASH_C,
    )

    monkeypatch.setattr(
        type(manifest),
        "state_of",
        lambda self, capability: ProviderManifestState.PROVEN,
    )
    with pytest.raises(
        ProviderCapabilityManifestError,
        match="canonical manifest state reader changed",
    ):
        manifest.supports(ProviderManifestCapability.STREAM)


def test_state_of_rejects_inplace_authority_reader_code_mutation(monkeypatch) -> None:
    profile = _profile()
    integration = bind_bookmaker_integration(
        profile,
        integration_kind=BookmakerIntegrationKind.OFFICIAL_API,
        observed_at=_T1,
        source_ref="integration-manifest",
        source_payload_sha256=_HASH_B,
    )
    manifest = build_provider_capability_manifest(
        profile,
        integration,
        manifest_ref="provider-capability-manifest",
        manifest_version=17,
        observed_at=_T2,
        source_ref="product-provider-capability-projection",
        source_payload_sha256=_HASH_C,
    )
    resolver = provider_manifest_module._canonical_capability_for

    def hostile_resolver(capability):
        return BookmakerCapability.LIVE_QUOTES_READ

    monkeypatch.setattr(resolver, "__code__", hostile_resolver.__code__)
    with pytest.raises(
        ProviderCapabilityManifestError,
        match="canonical manifest read authority changed",
    ):
        manifest.state_of(ProviderManifestCapability.STREAM)

