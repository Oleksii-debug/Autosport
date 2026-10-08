from dataclasses import fields, replace

import pytest

from autosport.bookmaker_capability import (
    BookmakerCapability,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
)
from autosport.bookmaker_integration_boundary import (
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
_T2 = "2026-09-21T13:02:00+00:00"
_HASH_A = "a" * 64
_HASH_B = "b" * 64
_HASH_C = "c" * 64


def _profile(
    *,
    facts: tuple[BookmakerCapabilityFact, ...] | None = None,
) -> BookmakerCapabilityProfile:
    if facts is None:
        facts = (
            BookmakerCapabilityFact(
                BookmakerCapability.ACCOUNT_IDENTITY_READ,
                BookmakerCapabilityState.SUPPORTED,
            ),
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
            BookmakerCapabilityFact(
                BookmakerCapability.CANCEL_BET,
                BookmakerCapabilityState.UNSUPPORTED,
            ),
        )
    return BookmakerCapabilityProfile(
        venue_id="betfair",
        account_id="acct-a",
        adapter_id="betfair-api",
        adapter_version="1.0",
        profile_version=3,
        facts=facts,
        observed_at=_T0,
        source_ref="capability-profile",
        source_payload_sha256=_HASH_A,
    )


def _integration(profile: BookmakerCapabilityProfile):
    return bind_bookmaker_integration(
        profile,
        integration_kind=BookmakerIntegrationKind.OFFICIAL_API,
        observed_at=_T1,
        source_ref="integration-manifest",
        source_payload_sha256=_HASH_B,
    )


def _evidence(
    kind: str = "provider-feature-probe",
    *,
    observed_at: str = _T1,
    profile: BookmakerCapabilityProfile | None = None,
) -> ProviderCapabilityEvidenceRef:
    profile = profile or _profile()
    integration = _integration(profile)
    return ProviderCapabilityEvidenceRef(
        kind=kind,
        evidence_ref=f"{kind}-v1",
        evidence_sha256=_HASH_C,
        observed_at=observed_at,
        profile_id=profile.profile_id,
        integration_evidence_id=integration.evidence_id,
    )


def _fact(
    capability: ProviderManifestCapability,
    *,
    state: ProviderManifestState = ProviderManifestState.PROVEN,
    values: tuple[str, ...] = (),
    observed_at: str = _T1,
    profile: BookmakerCapabilityProfile | None = None,
) -> ProviderCapabilityManifestFact:
    return ProviderCapabilityManifestFact(
        capability=capability,
        state=state,
        authority=ProviderManifestFactAuthority.EXPLICIT_EVIDENCE,
        values=values,
        evidence=_evidence(
            capability.value,
            observed_at=observed_at,
            profile=profile,
        ),
    )


def _manifest(
    *,
    profile: BookmakerCapabilityProfile | None = None,
    extension_facts: tuple[ProviderCapabilityManifestFact, ...] = (),
) -> ProviderCapabilityManifest:
    profile = profile or _profile()
    return build_provider_capability_manifest(
        profile,
        _integration(profile),
        manifest_ref="provider-capability-manifest",
        manifest_version=7,
        observed_at=_T2,
        source_ref="product-provider-capability-projection",
        source_payload_sha256=_HASH_C,
        extension_facts=extension_facts,
    )


def test_manifest_is_complete_and_missing_capabilities_are_not_proven() -> None:
    manifest = _manifest()

    assert tuple(fact.capability for fact in manifest.facts) == tuple(
        ProviderManifestCapability
    )
    assert manifest.integration_kind is BookmakerIntegrationKind.OFFICIAL_API
    assert (
        manifest.state_of(ProviderManifestCapability.LIVE_QUOTES)
        is ProviderManifestState.PROVEN
    )
    assert (
        manifest.state_of(ProviderManifestCapability.CANCEL_BET)
        is ProviderManifestState.UNSUPPORTED
    )
    for capability in (
        ProviderManifestCapability.SPORTS,
        ProviderManifestCapability.MARKETS,
        ProviderManifestCapability.POLL,
        ProviderManifestCapability.STREAM,
        ProviderManifestCapability.PROVIDER_TIMESTAMPS,
        ProviderManifestCapability.IMMEDIATE_ACK,
        ProviderManifestCapability.IDEMPOTENCY,
        ProviderManifestCapability.SETTLEMENT,
    ):
        assert manifest.state_of(capability) is ProviderManifestState.NOT_PROVEN


def test_every_canonical_bookmaker_capability_is_projected_exactly_once() -> None:
    mapping = {
        BookmakerCapability.ACCOUNT_IDENTITY_READ: ProviderManifestCapability.ACCOUNT_IDENTITY,
        BookmakerCapability.BALANCE_READ: ProviderManifestCapability.BALANCE,
        BookmakerCapability.LIMITS_READ: ProviderManifestCapability.LIMITS,
        BookmakerCapability.PREMATCH_QUOTES_READ: ProviderManifestCapability.PREMATCH_QUOTES,
        BookmakerCapability.LIVE_QUOTES_READ: ProviderManifestCapability.LIVE_QUOTES,
        BookmakerCapability.BETSLIP_READ: ProviderManifestCapability.BETSLIP,
        BookmakerCapability.OPEN_POSITIONS_READ: ProviderManifestCapability.OPEN_POSITIONS,
        BookmakerCapability.SETTLED_POSITIONS_READ: ProviderManifestCapability.SETTLED_POSITIONS,
        BookmakerCapability.PLACE_BET: ProviderManifestCapability.SUBMIT,
        BookmakerCapability.BET_READBACK: ProviderManifestCapability.READBACK,
        BookmakerCapability.CASHOUT: ProviderManifestCapability.CASHOUT,
        BookmakerCapability.CANCEL_BET: ProviderManifestCapability.CANCEL_BET,
    }

    assert set(mapping) == set(BookmakerCapability)
    manifest = _manifest()
    projected = {fact.capability: fact for fact in manifest.facts}
    for canonical, manifest_capability in mapping.items():
        fact = projected[manifest_capability]
        assert fact.authority is ProviderManifestFactAuthority.CANONICAL_PROFILE
        expected = {
            BookmakerCapabilityState.SUPPORTED: ProviderManifestState.PROVEN,
            BookmakerCapabilityState.UNSUPPORTED: ProviderManifestState.UNSUPPORTED,
            BookmakerCapabilityState.UNKNOWN: ProviderManifestState.NOT_PROVEN,
        }[_profile().state_of(canonical)]
        assert fact.state is expected


def test_canonical_capability_cannot_be_caller_overridden() -> None:
    override = ProviderCapabilityManifestFact(
        capability=ProviderManifestCapability.SUBMIT,
        state=ProviderManifestState.PROVEN,
        authority=ProviderManifestFactAuthority.CANONICAL_PROFILE,
    )
    profile = _profile(
        facts=(
            BookmakerCapabilityFact(
                BookmakerCapability.PLACE_BET,
                BookmakerCapabilityState.UNSUPPORTED,
            ),
        )
    )

    with pytest.raises(
        ProviderCapabilityManifestError,
        match="canonical and cannot be caller-overridden",
    ):
        _manifest(profile=profile, extension_facts=(override,))


def test_explicit_extension_evidence_is_structural_only_until_product_issuer_exists() -> None:
    fact = _fact(ProviderManifestCapability.STREAM)
    assert fact.authority is ProviderManifestFactAuthority.EXPLICIT_EVIDENCE
    assert fact.evidence is not None

    with pytest.raises(
        ProviderCapabilityManifestError,
        match="product-owned evidence authority",
    ):
        _manifest(extension_facts=(fact,))

    baseline = _manifest()
    direct_facts = tuple(
        fact if item.capability is ProviderManifestCapability.STREAM else item
        for item in baseline.facts
    )
    with pytest.raises(
        ProviderCapabilityManifestError,
        match="product-owned evidence authority",
    ):
        replace(baseline, facts=direct_facts)


def test_caller_evidence_cannot_mint_unsupported_extension_truth() -> None:
    fact = _fact(
        ProviderManifestCapability.STREAM,
        state=ProviderManifestState.UNSUPPORTED,
    )
    assert fact.evidence is not None
    with pytest.raises(
        ProviderCapabilityManifestError,
        match="product-owned evidence authority",
    ):
        _manifest(extension_facts=(fact,))


def test_sports_and_markets_structural_values_do_not_mint_capability_truth() -> None:
    with pytest.raises(ProviderCapabilityManifestError, match="requires concrete values"):
        _fact(ProviderManifestCapability.SPORTS)
    with pytest.raises(ProviderCapabilityManifestError, match="canonical sorted order"):
        _fact(
            ProviderManifestCapability.SPORTS,
            values=("tennis", "football"),
        )
    with pytest.raises(ProviderCapabilityManifestError, match="duplicates"):
        _fact(
            ProviderManifestCapability.MARKETS,
            values=("match_odds", "match_odds"),
        )

    sports = _fact(
        ProviderManifestCapability.SPORTS,
        values=("football", "tennis"),
    )
    with pytest.raises(
        ProviderCapabilityManifestError,
        match="product-owned evidence authority",
    ):
        _manifest(extension_facts=(sports,))


def test_noncoverage_capability_cannot_smuggle_values() -> None:
    with pytest.raises(ProviderCapabilityManifestError, match="only valid"):
        _fact(
            ProviderManifestCapability.STREAM,
            values=("football",),
        )


def test_transport_extensions_cannot_be_promoted_by_caller_evidence() -> None:
    for capability in (
        ProviderManifestCapability.POLL,
        ProviderManifestCapability.STREAM,
    ):
        with pytest.raises(
            ProviderCapabilityManifestError,
            match="product-owned evidence authority",
        ):
            _manifest(extension_facts=(_fact(capability),))


def test_execution_extensions_cannot_be_promoted_by_caller_evidence() -> None:
    for capability in (
        ProviderManifestCapability.IMMEDIATE_ACK,
        ProviderManifestCapability.IDEMPOTENCY,
    ):
        with pytest.raises(
            ProviderCapabilityManifestError,
            match="product-owned evidence authority",
        ):
            _manifest(extension_facts=(_fact(capability),))


def test_settlement_extension_cannot_be_promoted_by_caller_evidence() -> None:
    with pytest.raises(
        ProviderCapabilityManifestError,
        match="product-owned evidence authority",
    ):
        _manifest(
            extension_facts=(_fact(ProviderManifestCapability.SETTLEMENT),)
        )


def test_future_caller_evidence_cannot_reach_positive_authority() -> None:
    with pytest.raises(
        ProviderCapabilityManifestError,
        match="product-owned evidence authority",
    ):
        _manifest(
            extension_facts=(
                _fact(
                    ProviderManifestCapability.PROVIDER_TIMESTAMPS,
                    observed_at="2026-09-21T13:03:00+00:00",
                ),
            )
        )


def test_manifest_rejects_wrong_integration_profile_and_time_order() -> None:
    first = _profile()
    second = BookmakerCapabilityProfile(
        venue_id="betfair",
        account_id="acct-b",
        adapter_id="betfair-api",
        adapter_version="1.0",
        profile_version=3,
        facts=first.facts,
        observed_at=_T0,
        source_ref="other-profile",
        source_payload_sha256=_HASH_A,
    )
    with pytest.raises(BookmakerIntegrationEvidenceError, match="does not match"):
        build_provider_capability_manifest(
            first,
            _integration(second),
            manifest_ref="manifest",
            manifest_version=1,
            observed_at=_T2,
            source_ref="projection",
            source_payload_sha256=_HASH_C,
        )

    with pytest.raises(ProviderCapabilityManifestError, match="cannot predate"):
        build_provider_capability_manifest(
            first,
            _integration(first),
            manifest_ref="manifest",
            manifest_version=1,
            observed_at=_T0,
            source_ref="projection",
            source_payload_sha256=_HASH_C,
        )


def test_matching_identity_metadata_does_not_authenticate_extension_evidence() -> None:
    profile = _profile()
    fact = _fact(ProviderManifestCapability.STREAM, profile=profile)
    assert fact.evidence is not None
    assert fact.evidence.profile_id == profile.profile_id
    assert fact.evidence.integration_evidence_id == _integration(profile).evidence_id

    with pytest.raises(
        ProviderCapabilityManifestError,
        match="product-owned evidence authority",
    ):
        _manifest(profile=profile, extension_facts=(fact,))


def test_manifest_digest_is_deterministic_with_fail_closed_extensions() -> None:
    one = _manifest()
    two = _manifest()
    assert one.manifest_sha256 == two.manifest_sha256
    assert one.manifest_id == one.manifest_sha256
    assert len(one.manifest_sha256) == 64



def test_every_extension_conclusive_state_requires_product_owned_issuer() -> None:
    extension_capabilities = (
        ProviderManifestCapability.SPORTS,
        ProviderManifestCapability.MARKETS,
        ProviderManifestCapability.POLL,
        ProviderManifestCapability.STREAM,
        ProviderManifestCapability.PROVIDER_TIMESTAMPS,
        ProviderManifestCapability.IMMEDIATE_ACK,
        ProviderManifestCapability.IDEMPOTENCY,
        ProviderManifestCapability.SETTLEMENT,
    )
    baseline = _manifest()

    for capability in extension_capabilities:
        for state in (
            ProviderManifestState.PROVEN,
            ProviderManifestState.UNSUPPORTED,
        ):
            values = (
                ("football",)
                if state is ProviderManifestState.PROVEN
                and capability
                in {
                    ProviderManifestCapability.SPORTS,
                    ProviderManifestCapability.MARKETS,
                }
                else ()
            )
            fact = _fact(capability, state=state, values=values)

            with pytest.raises(
                ProviderCapabilityManifestError,
                match="product-owned evidence authority",
            ):
                _manifest(extension_facts=(fact,))

            direct_facts = tuple(
                fact if item.capability is capability else item
                for item in baseline.facts
            )
            with pytest.raises(
                ProviderCapabilityManifestError,
                match="product-owned evidence authority",
            ):
                replace(baseline, facts=direct_facts)


def test_duplicate_extension_fact_is_rejected() -> None:
    stream = _fact(ProviderManifestCapability.STREAM)
    with pytest.raises(ProviderCapabilityManifestError, match="duplicate extension"):
        _manifest(extension_facts=(stream, stream))


def test_direct_manifest_cannot_omit_or_reorder_fact_vocabulary() -> None:
    manifest = _manifest()
    with pytest.raises(ProviderCapabilityManifestError, match="every manifest capability"):
        replace(manifest, facts=manifest.facts[:-1])
    with pytest.raises(ProviderCapabilityManifestError, match="every manifest capability"):
        replace(
            manifest,
            facts=(manifest.facts[1], manifest.facts[0], *manifest.facts[2:]),
        )



def test_fact_authority_vocabulary_is_complete_and_exact() -> None:
    assert tuple(ProviderManifestFactAuthority) == (
        ProviderManifestFactAuthority.CANONICAL_PROFILE,
        ProviderManifestFactAuthority.EXPLICIT_EVIDENCE,
        ProviderManifestFactAuthority.NOT_PROVEN,
    )


def test_raw_string_enums_fail_closed() -> None:
    with pytest.raises(ProviderCapabilityManifestError, match="capability"):
        ProviderCapabilityManifestFact(
            capability="stream",
            state=ProviderManifestState.NOT_PROVEN,
            authority=ProviderManifestFactAuthority.NOT_PROVEN,
        )
    with pytest.raises(ProviderCapabilityManifestError, match="state"):
        ProviderCapabilityManifestFact(
            capability=ProviderManifestCapability.STREAM,
            state="not_proven",
            authority=ProviderManifestFactAuthority.NOT_PROVEN,
        )


def test_technical_submit_never_grants_execution_or_real_money_authority() -> None:
    manifest = _manifest()
    assert manifest.supports(ProviderManifestCapability.SUBMIT) is True
    assert manifest.provider_write_authorized is False
    assert manifest.execution_authorized is False
    assert manifest.real_money_execution is False


def test_contract_contains_no_secret_bearing_fields() -> None:
    names = {
        field.name
        for cls in (
            ProviderCapabilityEvidenceRef,
            ProviderCapabilityManifestFact,
            ProviderCapabilityManifest,
        )
        for field in fields(cls)
    }
    forbidden = {"password", "secret", "token", "cookie", "credential", "api_key"}
    assert not {
        name
        for name in names
        if any(fragment in name for fragment in forbidden)
    }
