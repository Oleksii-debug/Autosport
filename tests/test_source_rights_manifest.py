from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from autosport.source_rights_manifest import (
    SourceIdentity,
    SourcePurpose,
    SourceRightsDecisionCode,
    SourceRightsManifest,
    SourceRightsManifestError,
    SourceRightsReason,
    evaluate_source_rights,
)


T0 = datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(hours=1)
T2 = T0 + timedelta(hours=2)
T3 = T0 + timedelta(hours=3)


def _source(*, digit: str = "a", revision: str = "2026-09-21") -> SourceIdentity:
    return SourceIdentity(
        family="official-provider-export",
        source_id="provider:event-history",
        revision=revision,
        content_sha256=digit * 64,
    )


def _manifest(**overrides: object) -> SourceRightsManifest:
    values: dict[str, object] = {
        "manifest_id": "rights:provider:event-history:2026-09-21",
        "source": _source(),
        "authorization_artifact_id": "owner-approval:42",
        "authorization_artifact_sha256": "b" * 64,
        "permitted_purposes": (
            SourcePurpose.HISTORICAL_RESEARCH,
            SourcePurpose.MODEL_TRAINING,
        ),
        "approved_by": "owner:autosport",
        "approved_at": T0,
        "effective_at": T1,
        "expires_at": T3,
    }
    values.update(overrides)
    return SourceRightsManifest(**values)  # type: ignore[arg-type]


def test_exact_scope_and_active_window_allow_without_legal_conclusion() -> None:
    manifest = _manifest()
    decision = evaluate_source_rights(
        manifest,
        source=_source(),
        purpose=SourcePurpose.MODEL_TRAINING,
        use_at=T2,
    )
    assert decision.code is SourceRightsDecisionCode.ALLOW
    assert decision.reason is SourceRightsReason.AUTHORIZATION_MATCHED
    assert decision.allowed is True
    assert decision.legal_conclusion is False
    assert decision.manifest_sha256 == manifest.manifest_sha256
    assert decision.authorization_artifact_sha256 == "b" * 64


def test_purpose_scope_cannot_be_widened_by_caller() -> None:
    decision = evaluate_source_rights(
        _manifest(),
        source=_source(),
        purpose=SourcePurpose.LIVE_READ_ONLY,
        use_at=T2,
    )
    assert decision.code is SourceRightsDecisionCode.BLOCK
    assert decision.reason is SourceRightsReason.PURPOSE_NOT_PERMITTED


def test_source_identity_requires_exact_family_id_revision_and_bytes() -> None:
    manifest = _manifest()
    variants = (
        replace(_source(), family="browser-export"),
        replace(_source(), source_id="provider:other-history"),
        _source(revision="2026-09-22"),
        _source(digit="c"),
    )
    for source in variants:
        decision = evaluate_source_rights(
            manifest,
            source=source,
            purpose=SourcePurpose.MODEL_TRAINING,
            use_at=T2,
        )
        assert decision.code is SourceRightsDecisionCode.BLOCK
        assert decision.reason is SourceRightsReason.SOURCE_IDENTITY_MISMATCH


def test_effective_boundary_is_inclusive() -> None:
    decision = evaluate_source_rights(
        _manifest(),
        source=_source(),
        purpose=SourcePurpose.HISTORICAL_RESEARCH,
        use_at=T1,
    )
    assert decision.allowed is True


def test_before_effective_boundary_is_blocked() -> None:
    decision = evaluate_source_rights(
        _manifest(),
        source=_source(),
        purpose=SourcePurpose.HISTORICAL_RESEARCH,
        use_at=T1 - timedelta(microseconds=1),
    )
    assert decision.code is SourceRightsDecisionCode.BLOCK
    assert decision.reason is SourceRightsReason.AUTHORIZATION_NOT_YET_EFFECTIVE


def test_exact_expiry_boundary_is_blocked() -> None:
    decision = evaluate_source_rights(
        _manifest(),
        source=_source(),
        purpose=SourcePurpose.HISTORICAL_RESEARCH,
        use_at=T3,
    )
    assert decision.code is SourceRightsDecisionCode.BLOCK
    assert decision.reason is SourceRightsReason.AUTHORIZATION_EXPIRED


def test_last_instant_before_expiry_remains_allowed() -> None:
    decision = evaluate_source_rights(
        _manifest(),
        source=_source(),
        purpose=SourcePurpose.HISTORICAL_RESEARCH,
        use_at=T3 - timedelta(microseconds=1),
    )
    assert decision.allowed is True


def test_authorization_cannot_be_backdated_before_human_approval() -> None:
    with pytest.raises(SourceRightsManifestError, match="before human approval"):
        _manifest(approved_at=T2, effective_at=T1)


def test_expiry_must_be_strictly_after_effective_time() -> None:
    with pytest.raises(SourceRightsManifestError, match="after effective_at"):
        _manifest(expires_at=T1)


def test_naive_timestamps_are_invalid_evidence_not_positive_authority() -> None:
    raw = _manifest().to_dict()
    raw["approved_at"] = "2026-09-21T10:00:00"
    raw["manifest_sha256"] = "0" * 64
    decision = evaluate_source_rights(
        raw,
        source=_source(),
        purpose=SourcePurpose.MODEL_TRAINING,
        use_at=T2,
    )
    assert decision.code is SourceRightsDecisionCode.INVALID_EVIDENCE
    assert decision.reason is SourceRightsReason.MALFORMED_MANIFEST
    assert decision.manifest_sha256 is None


def test_non_utc_offsets_are_invalid_even_when_instant_is_equivalent() -> None:
    raw = _manifest().to_dict()
    raw["approved_at"] = "2026-09-21T12:00:00+02:00"
    raw["manifest_sha256"] = "0" * 64
    decision = evaluate_source_rights(
        raw,
        source=_source(),
        purpose=SourcePurpose.MODEL_TRAINING,
        use_at=T2,
    )
    assert decision.code is SourceRightsDecisionCode.INVALID_EVIDENCE


def test_duplicate_purposes_fail_closed() -> None:
    with pytest.raises(SourceRightsManifestError, match="unique"):
        _manifest(
            permitted_purposes=(
                SourcePurpose.MODEL_TRAINING,
                SourcePurpose.MODEL_TRAINING,
            )
        )


def test_unsorted_purposes_fail_closed_to_one_canonical_digest_order() -> None:
    with pytest.raises(SourceRightsManifestError, match="sorted canonically"):
        _manifest(
            permitted_purposes=(
                SourcePurpose.MODEL_TRAINING,
                SourcePurpose.HISTORICAL_RESEARCH,
            )
        )


def test_malformed_purpose_container_none_returns_invalid_without_digest_crash() -> None:
    raw = _manifest().to_dict()
    raw["permitted_purposes"] = None
    raw["manifest_sha256"] = "0" * 64
    decision = evaluate_source_rights(
        raw,
        source=_source(),
        purpose=SourcePurpose.MODEL_TRAINING,
        use_at=T2,
    )
    assert decision.code is SourceRightsDecisionCode.INVALID_EVIDENCE
    assert decision.manifest_sha256 is None


def test_direct_list_container_cannot_bypass_typed_manifest_validation() -> None:
    with pytest.raises(SourceRightsManifestError, match="non-empty tuple"):
        _manifest(permitted_purposes=[SourcePurpose.MODEL_TRAINING])


def test_unknown_purpose_enum_in_serialized_manifest_is_invalid_evidence() -> None:
    raw = _manifest().to_dict()
    raw["permitted_purposes"] = ["TRAIN_ANYTHING"]
    raw["manifest_sha256"] = "0" * 64
    decision = evaluate_source_rights(
        raw,
        source=_source(),
        purpose=SourcePurpose.MODEL_TRAINING,
        use_at=T2,
    )
    assert decision.code is SourceRightsDecisionCode.INVALID_EVIDENCE


def test_authorization_artifact_hash_must_be_canonical_lowercase_sha256() -> None:
    with pytest.raises(SourceRightsManifestError, match="lowercase SHA-256"):
        _manifest(authorization_artifact_sha256="B" * 64)
    with pytest.raises(SourceRightsManifestError, match="lowercase SHA-256"):
        _manifest(authorization_artifact_sha256="b" * 63)


def test_blank_or_whitespace_identity_fields_fail_closed() -> None:
    with pytest.raises(SourceRightsManifestError, match="canonical text"):
        _manifest(approved_by=" ")
    with pytest.raises(SourceRightsManifestError, match="canonical text"):
        SourceIdentity(
            family=" official-provider-export",
            source_id="provider:event-history",
            revision="2026-09-21",
            content_sha256="a" * 64,
        )


def test_roundtrip_requires_exact_digest_and_preserves_identity() -> None:
    manifest = _manifest()
    raw = manifest.to_dict()
    loaded = SourceRightsManifest.from_dict(raw)
    assert loaded == manifest
    assert loaded.manifest_sha256 == manifest.manifest_sha256

    raw["authorization_artifact_id"] = "owner-approval:forged"
    with pytest.raises(SourceRightsManifestError, match="digest mismatch"):
        SourceRightsManifest.from_dict(raw)


def test_manifest_digest_is_deterministic_and_binds_scope_source_and_artifact() -> None:
    manifest = _manifest()
    assert manifest.manifest_sha256 == _manifest().manifest_sha256
    variants = (
        _manifest(source=_source(digit="c")),
        _manifest(source=_source(revision="2026-09-22")),
        _manifest(authorization_artifact_sha256="c" * 64),
        _manifest(
            permitted_purposes=(
                SourcePurpose.HISTORICAL_RESEARCH,
                SourcePurpose.LIVE_READ_ONLY,
            )
        ),
    )
    assert all(item.manifest_sha256 != manifest.manifest_sha256 for item in variants)


def test_missing_manifest_field_returns_invalid_evidence() -> None:
    raw = _manifest().to_dict()
    del raw["authorization_artifact_id"]
    decision = evaluate_source_rights(
        raw,
        source=_source(),
        purpose=SourcePurpose.MODEL_TRAINING,
        use_at=T2,
    )
    assert decision.code is SourceRightsDecisionCode.INVALID_EVIDENCE


def test_non_mapping_manifest_returns_invalid_evidence() -> None:
    decision = evaluate_source_rights(
        object(),  # type: ignore[arg-type]
        source=_source(),
        purpose=SourcePurpose.MODEL_TRAINING,
        use_at=T2,
    )
    assert decision.code is SourceRightsDecisionCode.INVALID_EVIDENCE


def test_request_time_must_itself_be_explicit_utc() -> None:
    with pytest.raises(SourceRightsManifestError, match="timezone-aware"):
        evaluate_source_rights(
            _manifest(),
            source=_source(),
            purpose=SourcePurpose.MODEL_TRAINING,
            use_at=datetime(2026, 9, 21, 12, 0),
        )
    with pytest.raises(SourceRightsManifestError, match="UTC offset"):
        evaluate_source_rights(
            _manifest(),
            source=_source(),
            purpose=SourcePurpose.MODEL_TRAINING,
            use_at=datetime(
                2026,
                9,
                21,
                14,
                0,
                tzinfo=timezone(timedelta(hours=2)),
            ),
        )


def test_low_level_tampered_requested_source_is_revalidated() -> None:
    source = _source()
    object.__setattr__(source, "content_sha256", "not-a-sha")
    with pytest.raises(SourceRightsManifestError, match="lowercase SHA-256"):
        evaluate_source_rights(
            _manifest(),
            source=source,
            purpose=SourcePurpose.MODEL_TRAINING,
            use_at=T2,
        )


def test_low_level_tampered_manifest_source_is_invalid_evidence() -> None:
    manifest = _manifest()
    object.__setattr__(manifest.source, "source_id", " tampered")
    decision = evaluate_source_rights(
        manifest,
        source=_source(),
        purpose=SourcePurpose.MODEL_TRAINING,
        use_at=T2,
    )
    assert decision.code is SourceRightsDecisionCode.INVALID_EVIDENCE
    assert decision.reason is SourceRightsReason.MALFORMED_MANIFEST
    assert decision.manifest_sha256 is None
