from dataclasses import replace

import pytest

from autosport.bookmaker_capability import (
    BookmakerCapability,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
)
from autosport.bookmaker_integration_boundary import (
    BookmakerIntegrationEvidence,
    BookmakerIntegrationKind,
)
from autosport.provider_certification import (
    ProviderCapabilityProjection,
    ProviderCertificationError,
    ProviderCertificationEvidenceRef,
    ProviderCertifiedUse,
    ProviderRequalificationTrigger,
    ProviderTestMode,
    build_provider_certification,
)

D1 = "1" * 64
D2 = "2" * 64
D3 = "3" * 64
D4 = "4" * 64
D5 = "5" * 64
MANIFEST_REF = "provider-manifest:betfair:v3"
MANIFEST_VERSION = 3
MANIFEST_SHA = D5
CODE_REF = "git:cb102d85:src/autosport/betfair_adapter.py"
CONFIG_REF = "config:betfair-production-v1"


def _profile(
    *supported: BookmakerCapability,
    unsupported: tuple[BookmakerCapability, ...] = (),
) -> BookmakerCapabilityProfile:
    facts = tuple(
        [
            BookmakerCapabilityFact(c, BookmakerCapabilityState.SUPPORTED)
            for c in supported
        ]
        + [
            BookmakerCapabilityFact(c, BookmakerCapabilityState.UNSUPPORTED)
            for c in unsupported
        ]
    )
    return BookmakerCapabilityProfile(
        venue_id="betfair",
        account_id="account-A",
        adapter_id="betfair-rest-v1",
        adapter_version="1.2.3",
        profile_version=7,
        facts=facts,
        observed_at="2026-09-21T12:00:00+00:00",
        source_ref="provider-capability-observation:7",
        source_payload_sha256=D1,
    )


def _integration(profile: BookmakerCapabilityProfile) -> BookmakerIntegrationEvidence:
    return BookmakerIntegrationEvidence(
        venue_id=profile.venue_id,
        adapter_id=profile.adapter_id,
        adapter_version=profile.adapter_version,
        profile_id=profile.profile_id,
        integration_kind=BookmakerIntegrationKind.OFFICIAL_API,
        observed_at="2026-09-21T12:05:00+00:00",
        source_ref="official-api-contract:v1",
        source_payload_sha256=D2,
    )


def _ref(
    profile: BookmakerCapabilityProfile,
    integration: BookmakerIntegrationEvidence,
    mode: ProviderTestMode,
    *,
    evidence_id: str | None = None,
    digest: str = D3,
    observed_at: str = "2026-09-21T12:10:00+00:00",
    account_scope: str | None = None,
    code_ref: str = CODE_REF,
    code_sha: str = D1,
    config_ref: str = CONFIG_REF,
    config_sha: str = D2,
) -> ProviderCertificationEvidenceRef:
    return ProviderCertificationEvidenceRef(
        kind=f"{mode.value}-qualification",
        evidence_id=evidence_id or f"{mode.value}:run-1",
        evidence_sha256=digest,
        observed_at=observed_at,
        tested_mode=mode,
        account_scope=account_scope or profile.account_id,
        profile_id=profile.profile_id,
        integration_evidence_id=integration.evidence_id,
        capability_manifest_ref=MANIFEST_REF,
        capability_manifest_version=MANIFEST_VERSION,
        capability_manifest_sha256=MANIFEST_SHA,
        adapter_code_ref=code_ref,
        adapter_code_sha256=code_sha,
        adapter_config_ref=config_ref,
        adapter_config_sha256=config_sha,
    )


def _good_profile() -> BookmakerCapabilityProfile:
    return _profile(
        BookmakerCapability.ACCOUNT_IDENTITY_READ,
        BookmakerCapability.BALANCE_READ,
        BookmakerCapability.LIMITS_READ,
        BookmakerCapability.LIVE_QUOTES_READ,
        BookmakerCapability.BETSLIP_READ,
        BookmakerCapability.OPEN_POSITIONS_READ,
        BookmakerCapability.PLACE_BET,
        BookmakerCapability.BET_READBACK,
        unsupported=(BookmakerCapability.CASHOUT,),
    )


def _artifact(
    profile: BookmakerCapabilityProfile | None = None,
    modes: tuple[ProviderTestMode, ...] | None = None,
):
    profile = profile or _good_profile()
    integration = _integration(profile)
    modes = modes or (
        ProviderTestMode.SUPERVISED_EXECUTION,
        ProviderTestMode.REPLAY,
        ProviderTestMode.ACCOUNT_READ_ONLY,
        ProviderTestMode.OBSERVED_EXECUTABLE,
        ProviderTestMode.LIVE_OBSERVATION,
    )
    refs = tuple(
        _ref(
            profile,
            integration,
            mode,
            digest=D3 if index % 2 == 0 else D4,
            observed_at=f"2026-09-21T12:{10 + index:02d}:00+00:00",
        )
        for index, mode in enumerate(modes)
    )
    artifact = build_provider_certification(
        profile,
        integration,
        capability_manifest_ref=MANIFEST_REF,
        capability_manifest_version=MANIFEST_VERSION,
        capability_manifest_sha256=MANIFEST_SHA,
        adapter_code_ref=CODE_REF,
        adapter_code_sha256=D1,
        adapter_config_ref=CONFIG_REF,
        adapter_config_sha256=D2,
        tested_modes=modes,
        evidence_refs=tuple(reversed(refs)),
        limitations=(
            "No real-money authority",
            "Exact account scope only",
            "No real-money authority",
        ),
        issued_at="2026-09-21T12:20:00+00:00",
    )
    return profile, integration, artifact


def test_certification_projects_manifest_and_not_proven_capabilities():
    _, _, artifact = _artifact()
    assert artifact.profile_version == 7
    assert artifact.capability_manifest_version == MANIFEST_VERSION
    assert artifact.capability_manifest_ref == MANIFEST_REF
    assert artifact.capability_manifest_sha256 == MANIFEST_SHA
    assert tuple(item.capability for item in artifact.capability_states) == tuple(
        sorted(BookmakerCapability, key=lambda item: item.value)
    )
    assert BookmakerCapability.CANCEL_BET in artifact.unknown_capabilities
    assert BookmakerCapability.CASHOUT in artifact.unsupported_capabilities
    payload = artifact.to_canonical_dict()
    assert "cancel_bet" in payload["not_proven_capabilities"]
    assert artifact.limitations == (
        "Exact account scope only",
        "No real-money authority",
    )


def test_allowed_uses_require_mode_plus_exact_capability_prerequisites():
    _, _, artifact = _artifact()
    assert set(artifact.allowed_uses) == {
        ProviderCertifiedUse.REPLAY,
        ProviderCertifiedUse.LIVE_OBSERVATION,
        ProviderCertifiedUse.OBSERVED_EXECUTABLE,
        ProviderCertifiedUse.ACCOUNT_READ_ONLY,
        ProviderCertifiedUse.SUPERVISED_EXECUTION_CAPABLE,
    }

    profile = _profile(BookmakerCapability.LIVE_QUOTES_READ)
    _, _, limited = _artifact(
        profile,
        modes=(
            ProviderTestMode.LIVE_OBSERVATION,
            ProviderTestMode.OBSERVED_EXECUTABLE,
            ProviderTestMode.ACCOUNT_READ_ONLY,
            ProviderTestMode.SUPERVISED_EXECUTION,
        ),
    )
    assert limited.allowed_uses == (ProviderCertifiedUse.LIVE_OBSERVATION,)
    with pytest.raises(ProviderCertificationError):
        limited.require_use(ProviderCertifiedUse.SUPERVISED_EXECUTION_CAPABLE)


def test_direct_constructor_cannot_widen_allowed_use():
    _, _, artifact = _artifact(modes=(ProviderTestMode.REPLAY,))
    assert artifact.allowed_uses == (ProviderCertifiedUse.REPLAY,)
    with pytest.raises(ProviderCertificationError, match="mechanically derived"):
        replace(
            artifact,
            allowed_uses=(
                ProviderCertifiedUse.REPLAY,
                ProviderCertifiedUse.SUPERVISED_EXECUTION_CAPABLE,
            ),
        )


def test_every_tested_mode_requires_exact_bound_evidence():
    profile = _good_profile()
    integration = _integration(profile)
    replay = _ref(profile, integration, ProviderTestMode.REPLAY)
    with pytest.raises(ProviderCertificationError, match="every tested_mode"):
        build_provider_certification(
            profile,
            integration,
            capability_manifest_ref=MANIFEST_REF,
        capability_manifest_version=MANIFEST_VERSION,
        capability_manifest_sha256=MANIFEST_SHA,
        adapter_code_ref=CODE_REF,
            adapter_code_sha256=D1,
            adapter_config_ref=CONFIG_REF,
            adapter_config_sha256=D2,
            tested_modes=(ProviderTestMode.REPLAY, ProviderTestMode.LIVE_OBSERVATION),
            evidence_refs=(replay,),
            issued_at="2026-09-21T12:20:00+00:00",
        )


def test_qualification_evidence_cannot_be_rebound_to_another_account_or_code():
    profile = _good_profile()
    integration = _integration(profile)
    wrong_account = _ref(
        profile,
        integration,
        ProviderTestMode.REPLAY,
        account_scope="account-B",
    )
    with pytest.raises(ProviderCertificationError, match="exact certification identity"):
        build_provider_certification(
            profile,
            integration,
            capability_manifest_ref=MANIFEST_REF,
        capability_manifest_version=MANIFEST_VERSION,
        capability_manifest_sha256=MANIFEST_SHA,
        adapter_code_ref=CODE_REF,
            adapter_code_sha256=D1,
            adapter_config_ref=CONFIG_REF,
            adapter_config_sha256=D2,
            tested_modes=(ProviderTestMode.REPLAY,),
            evidence_refs=(wrong_account,),
            issued_at="2026-09-21T12:20:00+00:00",
        )

    wrong_code = _ref(
        profile,
        integration,
        ProviderTestMode.REPLAY,
        code_ref="git:other:adapter.py",
    )
    with pytest.raises(ProviderCertificationError, match="exact certification identity"):
        build_provider_certification(
            profile,
            integration,
            capability_manifest_ref=MANIFEST_REF,
        capability_manifest_version=MANIFEST_VERSION,
        capability_manifest_sha256=MANIFEST_SHA,
        adapter_code_ref=CODE_REF,
            adapter_code_sha256=D1,
            adapter_config_ref=CONFIG_REF,
            adapter_config_sha256=D2,
            tested_modes=(ProviderTestMode.REPLAY,),
            evidence_refs=(wrong_code,),
            issued_at="2026-09-21T12:20:00+00:00",
        )


def test_certification_identity_is_deterministic_under_input_order():
    profile, integration, first = _artifact()
    second = build_provider_certification(
        profile,
        integration,
        capability_manifest_ref=MANIFEST_REF,
        capability_manifest_version=MANIFEST_VERSION,
        capability_manifest_sha256=MANIFEST_SHA,
        adapter_code_ref=CODE_REF,
        adapter_code_sha256=D1,
        adapter_config_ref=CONFIG_REF,
        adapter_config_sha256=D2,
        tested_modes=tuple(reversed(first.tested_modes)),
        evidence_refs=tuple(reversed(first.evidence_refs)),
        limitations=tuple(reversed(first.limitations)),
        issued_at=first.issued_at,
    )
    assert second.to_canonical_dict() == first.to_canonical_dict()
    assert second.artifact_id == first.artifact_id


def test_verify_current_fails_closed_on_code_config_and_test_evidence_drift():
    profile, integration, artifact = _artifact()
    artifact.verify_current(
        profile,
        integration,
        capability_manifest_ref=MANIFEST_REF,
        capability_manifest_version=MANIFEST_VERSION,
        capability_manifest_sha256=MANIFEST_SHA,
        adapter_code_ref=CODE_REF,
        adapter_code_sha256=D1,
        adapter_config_ref=CONFIG_REF,
        adapter_config_sha256=D2,
        evidence_refs=tuple(reversed(artifact.evidence_refs)),
    )
    assert artifact.requalification_triggers == tuple(ProviderRequalificationTrigger)

    cases = (
        ({"capability_manifest_ref": "provider-manifest:betfair:v4"}, "manifest drift"),
        ({"capability_manifest_version": 4}, "manifest drift"),
        ({"capability_manifest_sha256": D4}, "manifest drift"),
        ({"adapter_code_ref": "git:other:adapter.py"}, "code reference drift"),
        ({"adapter_code_sha256": D4}, "code drift"),
        ({"adapter_config_ref": "config:other"}, "config reference drift"),
        ({"adapter_config_sha256": D4}, "config drift"),
    )
    for overrides, message in cases:
        args = {
            "capability_manifest_ref": MANIFEST_REF,
            "capability_manifest_version": MANIFEST_VERSION,
            "capability_manifest_sha256": MANIFEST_SHA,
            "adapter_code_ref": CODE_REF,
            "adapter_code_sha256": D1,
            "adapter_config_ref": CONFIG_REF,
            "adapter_config_sha256": D2,
            "evidence_refs": artifact.evidence_refs,
        }
        args.update(overrides)
        with pytest.raises(ProviderCertificationError, match=message):
            artifact.verify_current(profile, integration, **args)

    changed_refs = (
        replace(artifact.evidence_refs[0], evidence_sha256=D1),
        *artifact.evidence_refs[1:],
    )
    with pytest.raises(ProviderCertificationError, match="test evidence drift"):
        artifact.verify_current(
            profile,
            integration,
            capability_manifest_ref=MANIFEST_REF,
        capability_manifest_version=MANIFEST_VERSION,
        capability_manifest_sha256=MANIFEST_SHA,
        adapter_code_ref=CODE_REF,
            adapter_code_sha256=D1,
            adapter_config_ref=CONFIG_REF,
            adapter_config_sha256=D2,
            evidence_refs=changed_refs,
        )


def test_profile_integration_or_projection_drift_requires_requalification():
    profile, integration, artifact = _artifact()
    changed_profile = replace(profile, profile_version=8)
    with pytest.raises(ProviderCertificationError, match="integration evidence no longer binds"):
        artifact.verify_current(
            changed_profile,
            integration,
            capability_manifest_ref=MANIFEST_REF,
        capability_manifest_version=MANIFEST_VERSION,
        capability_manifest_sha256=MANIFEST_SHA,
        adapter_code_ref=CODE_REF,
            adapter_code_sha256=D1,
            adapter_config_ref=CONFIG_REF,
            adapter_config_sha256=D2,
            evidence_refs=artifact.evidence_refs,
        )

    changed_integration = replace(
        integration,
        integration_kind=BookmakerIntegrationKind.BROWSER_AUTOMATION,
    )
    with pytest.raises(ProviderCertificationError, match="profile/integration drift"):
        artifact.verify_current(
            profile,
            changed_integration,
            capability_manifest_ref=MANIFEST_REF,
        capability_manifest_version=MANIFEST_VERSION,
        capability_manifest_sha256=MANIFEST_SHA,
        adapter_code_ref=CODE_REF,
            adapter_code_sha256=D1,
            adapter_config_ref=CONFIG_REF,
            adapter_config_sha256=D2,
            evidence_refs=artifact.evidence_refs,
        )

    forged_states = tuple(
        ProviderCapabilityProjection(
            item.capability,
            BookmakerCapabilityState.SUPPORTED
            if item.capability is BookmakerCapability.CANCEL_BET
            else item.state,
        )
        for item in artifact.capability_states
    )
    forged = replace(artifact, capability_states=forged_states)
    with pytest.raises(ProviderCertificationError, match="projection does not match"):
        forged.verify_current(
            profile,
            integration,
            capability_manifest_ref=MANIFEST_REF,
        capability_manifest_version=MANIFEST_VERSION,
        capability_manifest_sha256=MANIFEST_SHA,
        adapter_code_ref=CODE_REF,
            adapter_code_sha256=D1,
            adapter_config_ref=CONFIG_REF,
            adapter_config_sha256=D2,
            evidence_refs=artifact.evidence_refs,
        )


def test_builder_rejects_future_or_predating_qualification_evidence():
    profile = _profile(BookmakerCapability.LIVE_QUOTES_READ)
    integration = _integration(profile)
    future = _ref(
        profile,
        integration,
        ProviderTestMode.LIVE_OBSERVATION,
        observed_at="2026-09-21T13:00:00+00:00",
    )
    with pytest.raises(ProviderCertificationError, match="cannot postdate"):
        build_provider_certification(
            profile,
            integration,
            capability_manifest_ref=MANIFEST_REF,
        capability_manifest_version=MANIFEST_VERSION,
        capability_manifest_sha256=MANIFEST_SHA,
        adapter_code_ref=CODE_REF,
            adapter_code_sha256=D1,
            adapter_config_ref=CONFIG_REF,
            adapter_config_sha256=D2,
            tested_modes=(ProviderTestMode.LIVE_OBSERVATION,),
            evidence_refs=(future,),
            issued_at="2026-09-21T12:20:00+00:00",
        )

    old = _ref(
        profile,
        integration,
        ProviderTestMode.REPLAY,
        observed_at="2026-09-21T12:01:00+00:00",
    )
    with pytest.raises(ProviderCertificationError, match="cannot predate integration"):
        build_provider_certification(
            profile,
            integration,
            capability_manifest_ref=MANIFEST_REF,
        capability_manifest_version=MANIFEST_VERSION,
        capability_manifest_sha256=MANIFEST_SHA,
        adapter_code_ref=CODE_REF,
            adapter_code_sha256=D1,
            adapter_config_ref=CONFIG_REF,
            adapter_config_sha256=D2,
            tested_modes=(ProviderTestMode.REPLAY,),
            evidence_refs=(old,),
            issued_at="2026-09-21T12:20:00+00:00",
        )


def test_conflicting_evidence_identity_is_rejected():
    profile = _good_profile()
    integration = _integration(profile)
    first = _ref(
        profile,
        integration,
        ProviderTestMode.REPLAY,
        evidence_id="same-run",
        digest=D3,
    )
    conflicting = replace(first, evidence_sha256=D4)
    with pytest.raises(ProviderCertificationError, match="conflicting versions"):
        build_provider_certification(
            profile,
            integration,
            capability_manifest_ref=MANIFEST_REF,
        capability_manifest_version=MANIFEST_VERSION,
        capability_manifest_sha256=MANIFEST_SHA,
        adapter_code_ref=CODE_REF,
            adapter_code_sha256=D1,
            adapter_config_ref=CONFIG_REF,
            adapter_config_sha256=D2,
            tested_modes=(ProviderTestMode.REPLAY,),
            evidence_refs=(first, conflicting),
            issued_at="2026-09-21T12:20:00+00:00",
        )


def test_artifact_exposes_technical_capability_not_execution_authority():
    _, _, artifact = _artifact()
    artifact.require_use(ProviderCertifiedUse.SUPERVISED_EXECUTION_CAPABLE)
    payload = artifact.to_canonical_dict()
    for forbidden in (
        "execution_authorized",
        "real_money_execution",
        "settlement_authority",
        "risk_authority",
        "legal_permission",
    ):
        assert forbidden not in payload
