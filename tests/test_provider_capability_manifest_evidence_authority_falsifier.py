import pytest

import autosport.provider_capability_manifest as provider_manifest_module

from autosport.bookmaker_capability import (
    BookmakerCapability,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
)
from autosport.bookmaker_integration_boundary import (
    BookmakerIntegrationEvidence,
    BookmakerIntegrationEvidenceError,
    BookmakerIntegrationKind,
    bind_bookmaker_integration,
)
from autosport.provider_capability_manifest import (
    ProviderCapabilityEvidenceRef,
    ProviderCapabilityManifest,
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

    with pytest.raises(
        BookmakerIntegrationEvidenceError,
        match="does not match capability profile identity",
    ):
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

def test_manifest_identity_rejects_postconstruction_extension_mutation() -> None:
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
        manifest_version=18,
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
        match="product-owned evidence authority",
    ):
        _ = manifest.manifest_id


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("manifest_version", 0, "positive integer"),
        ("source_payload_sha256", "not-a-digest", "SHA-256"),
    ),
)
def test_manifest_identity_revalidates_mutated_scalar_contract(
    field: str,
    value: object,
    message: str,
) -> None:
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
        manifest_version=19,
        observed_at=_T2,
        source_ref="product-provider-capability-projection",
        source_payload_sha256=_HASH_C,
    )
    object.__setattr__(manifest, field, value)

    with pytest.raises(ProviderCapabilityManifestError, match=message):
        _ = manifest.manifest_sha256


def test_integration_kind_revalidates_mutated_integration_contract() -> None:
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
        manifest_version=20,
        observed_at=_T2,
        source_ref="product-provider-capability-projection",
        source_payload_sha256=_HASH_C,
    )
    object.__setattr__(integration, "integration_kind", "official_api")

    with pytest.raises(
        BookmakerIntegrationEvidenceError,
        match="integration_kind",
    ):
        _ = manifest.integration_kind


def test_identity_surfaces_reject_dependency_validator_rebinding(
    monkeypatch,
) -> None:
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
        manifest_version=21,
        observed_at=_T2,
        source_ref="product-provider-capability-projection",
        source_payload_sha256=_HASH_C,
    )

    monkeypatch.setattr(
        BookmakerIntegrationEvidence,
        "verify_profile",
        lambda self, candidate: None,
    )
    with pytest.raises(
        ProviderCapabilityManifestError,
        match="canonical manifest validator changed|canonical manifest dependency validator changed",
    ):
        _ = manifest.manifest_id

def test_manifest_authority_class_dispatch_cannot_be_rebound() -> None:
    critical_names = (
        "__init__",
        "__post_init__",
        "_validate_facts",
        "_validate_dependencies",
        "state_of",
        "supports",
        "integration_kind",
        "manifest_sha256",
        "manifest_id",
        "provider_write_authorized",
        "execution_authorized",
        "real_money_execution",
        "to_canonical_dict",
        "__setattr__",
        "__delattr__",
    )

    for name in critical_names:
        original = ProviderCapabilityManifest.__dict__[name]
        try:
            with pytest.raises(TypeError, match="sealed provider-manifest authority"):
                setattr(ProviderCapabilityManifest, name, lambda *args, **kwargs: True)
        finally:
            if ProviderCapabilityManifest.__dict__.get(name) is not original:
                type.__setattr__(ProviderCapabilityManifest, name, original)


def test_manifest_authority_class_dispatch_cannot_be_deleted() -> None:
    critical_names = (
        "__post_init__",
        "_validate_facts",
        "state_of",
        "supports",
        "execution_authorized",
        "real_money_execution",
        "to_canonical_dict",
    )

    for name in critical_names:
        original = ProviderCapabilityManifest.__dict__[name]
        try:
            with pytest.raises(TypeError, match="sealed provider-manifest authority"):
                delattr(ProviderCapabilityManifest, name)
        finally:
            if name not in ProviderCapabilityManifest.__dict__:
                type.__setattr__(ProviderCapabilityManifest, name, original)

def test_coordinated_profile_integration_fact_mutation_cannot_rebind_manifest() -> None:
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
        manifest_version=22,
        observed_at=_T2,
        source_ref="product-provider-capability-projection",
        source_payload_sha256=_HASH_C,
    )
    live_profile_fact = next(
        fact
        for fact in profile.facts
        if fact.capability is BookmakerCapability.LIVE_QUOTES_READ
    )
    live_manifest_fact = next(
        fact
        for fact in manifest.facts
        if fact.capability is ProviderManifestCapability.LIVE_QUOTES
    )

    object.__setattr__(
        live_profile_fact,
        "state",
        BookmakerCapabilityState.UNSUPPORTED,
    )
    object.__setattr__(integration, "profile_id", profile.profile_id)
    object.__setattr__(
        live_manifest_fact,
        "state",
        ProviderManifestState.UNSUPPORTED,
    )

    with pytest.raises(
        ProviderCapabilityManifestError,
        match="bound capability profile identity changed after validation",
    ):
        manifest.state_of(ProviderManifestCapability.LIVE_QUOTES)


def test_valid_integration_kind_mutation_cannot_rebind_manifest_identity() -> None:
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
        manifest_version=23,
        observed_at=_T2,
        source_ref="product-provider-capability-projection",
        source_payload_sha256=_HASH_C,
    )
    object.__setattr__(
        integration,
        "integration_kind",
        BookmakerIntegrationKind.BROWSER_AUTOMATION,
    )

    with pytest.raises(
        ProviderCapabilityManifestError,
        match="bound integration evidence identity changed after validation",
    ):
        _ = manifest.integration_kind


def test_manifest_identity_rejects_coordinated_dependency_rebinding() -> None:
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
        manifest_version=24,
        observed_at=_T2,
        source_ref="product-provider-capability-projection",
        source_payload_sha256=_HASH_C,
    )
    live_profile_fact = next(
        fact
        for fact in profile.facts
        if fact.capability is BookmakerCapability.LIVE_QUOTES_READ
    )
    live_manifest_fact = next(
        fact
        for fact in manifest.facts
        if fact.capability is ProviderManifestCapability.LIVE_QUOTES
    )
    object.__setattr__(
        live_profile_fact,
        "state",
        BookmakerCapabilityState.UNSUPPORTED,
    )
    object.__setattr__(integration, "profile_id", profile.profile_id)
    object.__setattr__(
        live_manifest_fact,
        "state",
        ProviderManifestState.UNSUPPORTED,
    )

    with pytest.raises(
        ProviderCapabilityManifestError,
        match="bound capability profile identity changed after validation",
    ):
        _ = manifest.manifest_id

def test_manifest_identity_rejects_internal_validation_dispatch_bypass(
    monkeypatch,
) -> None:
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
        manifest_version=25,
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

    monkeypatch.setattr(type(manifest), "_validate_facts", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        type(manifest),
        "_validate_dependencies",
        lambda *args, **kwargs: None,
    )

    with pytest.raises(
        ProviderCapabilityManifestError,
        match="stream extension truth changed after validation",
    ):
        _ = manifest.manifest_id


def test_manifest_identity_rejects_state_reader_rebinding(monkeypatch) -> None:
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
        manifest_version=26,
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
        match="canonical manifest validator changed",
    ):
        _ = manifest.manifest_id

def test_bound_identity_fields_cannot_be_rewritten_with_coordinated_truth_mutation() -> None:
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
        manifest_version=25,
        observed_at=_T2,
        source_ref="product-provider-capability-projection",
        source_payload_sha256=_HASH_C,
    )
    prematch_manifest_fact = next(
        fact
        for fact in manifest.facts
        if fact.capability is ProviderManifestCapability.PREMATCH_QUOTES
    )
    assert manifest.state_of(ProviderManifestCapability.PREMATCH_QUOTES) is (
        ProviderManifestState.NOT_PROVEN
    )

    widened_profile_facts = profile.facts + (
        BookmakerCapabilityFact(
            BookmakerCapability.PREMATCH_QUOTES_READ,
            BookmakerCapabilityState.SUPPORTED,
        ),
    )
    object.__setattr__(profile, "facts", widened_profile_facts)
    object.__setattr__(integration, "profile_id", profile.profile_id)
    object.__setattr__(
        prematch_manifest_fact,
        "state",
        ProviderManifestState.PROVEN,
    )
    object.__setattr__(manifest, "_bound_profile_id", profile.profile_id)
    object.__setattr__(
        manifest,
        "_bound_integration_evidence_id",
        integration.evidence_id,
    )

    with pytest.raises(
        ProviderCapabilityManifestError,
        match="bound capability profile identity changed after validation",
    ):
        manifest.state_of(ProviderManifestCapability.PREMATCH_QUOTES)

