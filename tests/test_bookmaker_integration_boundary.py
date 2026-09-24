from dataclasses import fields, replace

import pytest

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


_PROFILE_TS = "2026-09-21T07:40:00+00:00"
_EVIDENCE_TS = "2026-09-21T07:41:00+00:00"
_HASH = "a" * 64
_SOURCE_HASH = "b" * 64


def _profile(**overrides) -> BookmakerCapabilityProfile:
    values = {
        "venue_id": "book-a",
        "account_id": "acct-a",
        "adapter_id": "adapter-a",
        "adapter_version": "1.0",
        "profile_version": 1,
        "facts": (
            BookmakerCapabilityFact(
                BookmakerCapability.BALANCE_READ,
                BookmakerCapabilityState.SUPPORTED,
            ),
        ),
        "observed_at": _PROFILE_TS,
        "source_ref": "capability-probe",
        "source_payload_sha256": _HASH,
    }
    values.update(overrides)
    return BookmakerCapabilityProfile(**values)


def _evidence(
    profile: BookmakerCapabilityProfile | None = None,
    *,
    kind: BookmakerIntegrationKind = BookmakerIntegrationKind.OFFICIAL_API,
    **overrides,
) -> BookmakerIntegrationEvidence:
    profile = profile or _profile()
    values = {
        "venue_id": profile.venue_id,
        "adapter_id": profile.adapter_id,
        "adapter_version": profile.adapter_version,
        "profile_id": profile.profile_id,
        "integration_kind": kind,
        "observed_at": _EVIDENCE_TS,
        "source_ref": "adapter-integration-manifest",
        "source_payload_sha256": _SOURCE_HASH,
    }
    values.update(overrides)
    return BookmakerIntegrationEvidence(**values)


def test_official_api_evidence_binds_exact_capability_profile() -> None:
    profile = _profile()
    evidence = bind_bookmaker_integration(
        profile,
        integration_kind=BookmakerIntegrationKind.OFFICIAL_API,
        observed_at=_EVIDENCE_TS,
        source_ref="official-api-adapter-manifest",
        source_payload_sha256=_SOURCE_HASH,
    )

    evidence.verify_profile(profile)
    assert evidence.integration_kind is BookmakerIntegrationKind.OFFICIAL_API
    assert evidence.profile_id == profile.profile_id
    assert len(evidence.evidence_id) == 64
    assert evidence.evidence_id == replace(evidence).evidence_id


def test_browser_channel_is_explicit_but_never_changes_capability_truth() -> None:
    profile = _profile()
    official = _evidence(profile, kind=BookmakerIntegrationKind.OFFICIAL_API)
    browser = _evidence(profile, kind=BookmakerIntegrationKind.BROWSER_AUTOMATION)

    official.verify_profile(profile)
    browser.verify_profile(profile)
    assert official.evidence_id != browser.evidence_id
    assert profile.supports(BookmakerCapability.BALANCE_READ) is True
    assert profile.supports(BookmakerCapability.PLACE_BET) is False
    assert profile.state_of(BookmakerCapability.PLACE_BET) is BookmakerCapabilityState.UNKNOWN


def test_evidence_fails_closed_on_profile_identity_or_version_drift() -> None:
    profile = _profile()
    evidence = _evidence(profile)

    for changed in (
        _profile(venue_id="book-b"),
        _profile(adapter_id="adapter-b"),
        _profile(adapter_version="2.0"),
        _profile(profile_version=2),
    ):
        with pytest.raises(
            BookmakerIntegrationEvidenceError,
            match="does not match capability profile identity",
        ):
            evidence.verify_profile(changed)


def test_integration_evidence_cannot_predate_bound_profile() -> None:
    profile = _profile(observed_at="2026-09-21T07:42:00+00:00")
    evidence = _evidence(profile, observed_at=_EVIDENCE_TS)

    with pytest.raises(BookmakerIntegrationEvidenceError, match="cannot predate"):
        evidence.verify_profile(profile)
    with pytest.raises(BookmakerIntegrationEvidenceError, match="cannot predate"):
        bind_bookmaker_integration(
            profile,
            integration_kind=BookmakerIntegrationKind.BROWSER_AUTOMATION,
            observed_at=_EVIDENCE_TS,
            source_ref="browser-adapter-manifest",
            source_payload_sha256=_SOURCE_HASH,
        )


def test_malformed_channel_evidence_is_rejected() -> None:
    profile = _profile()
    with pytest.raises(BookmakerIntegrationEvidenceError, match="integration_kind"):
        _evidence(profile, integration_kind="official_api")
    with pytest.raises(BookmakerIntegrationEvidenceError, match="timezone"):
        _evidence(profile, observed_at="2026-09-21T07:41:00")
    with pytest.raises(BookmakerIntegrationEvidenceError, match="SHA-256"):
        _evidence(profile, source_payload_sha256="not-a-hash")
    with pytest.raises(BookmakerIntegrationEvidenceError, match="schema_version"):
        _evidence(profile, schema_version=2)


def test_contract_has_no_secret_or_execution_bearing_fields() -> None:
    names = {field.name for field in fields(BookmakerIntegrationEvidence)}
    forbidden_fragments = {
        "password",
        "secret",
        "token",
        "cookie",
        "credential",
        "stake",
        "order",
        "execution",
        "real_money",
        "legal_permission",
    }

    assert not {
        name
        for name in names
        if any(fragment in name for fragment in forbidden_fragments)
    }
