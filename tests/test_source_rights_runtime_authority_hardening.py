import json
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path

import pytest

import autosport.source_rights_manifest as subject
from autosport.source_rights_manifest import (
    SourceRightsAuthorization,
    SourceRightsManifest,
    SourceRightsManifestError,
    authorize_source_use,
    load_source_rights_manifest,
)

NOW = datetime(2026, 9, 21, tzinfo=timezone.utc)


def payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "autosport_source_rights_manifest",
        "source_identity": "licensed-provider-account:fixture",
        "authorized_scopes": [
            "historical.internal_research",
            "historical.read",
        ],
        "effective_at": "2026-09-01T00:00:00Z",
        "expires_at": "2026-12-01T00:00:00Z",
        "human_approved": True,
        "approved_by": "release-owner",
        "approval_reference": "entitlement-record:fixture-001",
        "approved_at": "2026-08-31T12:00:00Z",
    }


def manifest(tmp_path: Path) -> SourceRightsManifest:
    path = tmp_path / "rights.json"
    path.write_text(json.dumps(payload()), encoding="utf-8")
    return load_source_rights_manifest(path)


def authorize(current: SourceRightsManifest) -> SourceRightsAuthorization:
    return authorize_source_use(
        current,
        source_identity=current.source_identity,
        required_scope="historical.read",
        at=NOW,
    )


def test_valid_manifest_still_authorizes(tmp_path: Path) -> None:
    result = authorize(manifest(tmp_path))
    assert result.required_scope == "historical.read"


def test_caller_cannot_mint_positive_source_rights_authorization() -> None:
    with pytest.raises(SourceRightsManifestError, match="only be issued"):
        SourceRightsAuthorization(
            source_identity="unverified-source",
            required_scope="historical.read",
            checked_at=NOW,
            manifest_sha256="0" * 64,
            approved_by="release-owner",
            approval_reference="caller-authored-reference",
        )


@pytest.mark.parametrize(
    ("field_name", "replacement"),
    (
        ("source_identity", "forged-source"),
        ("required_scope", "live.write"),
        ("manifest_sha256", "0" * 64),
    ),
)
def test_issued_authorization_fields_cannot_be_rewritten(
    tmp_path: Path,
    field_name: str,
    replacement: object,
) -> None:
    decision = authorize(manifest(tmp_path))
    original = getattr(decision, field_name)

    with pytest.raises(AttributeError):
        object.__setattr__(decision, field_name, replacement)

    assert getattr(decision, field_name) == original


def test_string_subclasses_cannot_execute_callbacks(tmp_path: Path) -> None:
    current = manifest(tmp_path)

    class ExplodingStr(str):
        def strip(self) -> str:
            raise RuntimeError("callback executed")

    with pytest.raises(SourceRightsManifestError):
        authorize_source_use(
            current,
            source_identity=ExplodingStr(current.source_identity),
            required_scope="historical.read",
            at=NOW,
        )
    with pytest.raises(SourceRightsManifestError):
        authorize_source_use(
            current,
            source_identity=current.source_identity,
            required_scope=ExplodingStr("historical.read"),
            at=NOW,
        )


def test_datetime_subclass_cannot_execute_callback(tmp_path: Path) -> None:
    current = manifest(tmp_path)

    class ExplodingDatetime(datetime):
        def utcoffset(self):
            raise RuntimeError("callback executed")

    attack = ExplodingDatetime(2026, 9, 21, tzinfo=timezone.utc)
    with pytest.raises(SourceRightsManifestError):
        authorize_source_use(
            current,
            source_identity=current.source_identity,
            required_scope="historical.read",
            at=attack,
        )


def test_custom_tzinfo_cannot_execute_callback(tmp_path: Path) -> None:
    current = manifest(tmp_path)

    class ExplodingTZ(tzinfo):
        def utcoffset(self, dt):
            raise RuntimeError("callback executed")

    attack = datetime(2026, 9, 21, tzinfo=ExplodingTZ())
    with pytest.raises(SourceRightsManifestError):
        authorize_source_use(
            current,
            source_identity=current.source_identity,
            required_scope="historical.read",
            at=attack,
        )


def test_manifest_subclass_rejected_before_attribute_access(tmp_path: Path) -> None:
    current = manifest(tmp_path)

    class ExplodingManifest(SourceRightsManifest):
        def __getattribute__(self, name):
            raise RuntimeError("callback executed")

    attack = object.__new__(ExplodingManifest)
    with pytest.raises(SourceRightsManifestError, match="exact verified"):
        authorize_source_use(
            attack,
            source_identity=current.source_identity,
            required_scope="historical.read",
            at=NOW,
        )


def test_runtime_field_subclass_rejected_without_callback(tmp_path: Path) -> None:
    current = manifest(tmp_path)

    class ExplodingStr(str):
        def strip(self) -> str:
            raise RuntimeError("callback executed")

    object.__setattr__(
        current,
        "source_identity",
        ExplodingStr(current.source_identity),
    )
    with pytest.raises(SourceRightsManifestError):
        authorize_source_use(
            current,
            source_identity="licensed-provider-account:fixture",
            required_scope="historical.read",
            at=NOW,
        )


def test_non_utc_manifest_timestamp_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "rights.json"
    raw = payload()
    raw["effective_at"] = "2026-09-01T02:00:00+02:00"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(SourceRightsManifestError, match="must use UTC"):
        load_source_rights_manifest(path)


def test_non_utc_authorization_time_is_rejected(tmp_path: Path) -> None:
    current = manifest(tmp_path)
    non_utc = datetime(
        2026,
        9,
        21,
        2,
        tzinfo=timezone(timedelta(hours=2)),
    )
    with pytest.raises(SourceRightsManifestError, match="must use UTC"):
        authorize_source_use(
            current,
            source_identity=current.source_identity,
            required_scope="historical.read",
            at=non_utc,
        )


def test_tampered_digest_type_fails_closed(tmp_path: Path) -> None:
    current = manifest(tmp_path)

    class ExplodingStr(str):
        def strip(self) -> str:
            raise RuntimeError("callback executed")

    object.__setattr__(
        current,
        "manifest_sha256",
        ExplodingStr(current.manifest_sha256),
    )
    with pytest.raises(SourceRightsManifestError):
        authorize_source_use(
            current,
            source_identity=current.source_identity,
            required_scope="historical.read",
            at=NOW,
        )



def test_parsed_scope_cardinality_is_bounded_before_sorting(tmp_path: Path) -> None:
    path = tmp_path / "rights.json"
    raw = payload()
    raw["authorized_scopes"] = [
        f"scope.{index}" for index in range(subject._MAX_AUTHORIZED_SCOPES + 1)
    ]
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(SourceRightsManifestError, match="maximum supported scope count"):
        load_source_rights_manifest(path)


def test_runtime_scope_cardinality_is_bounded_before_revalidation(tmp_path: Path) -> None:
    current = manifest(tmp_path)
    object.__setattr__(
        current,
        "authorized_scopes",
        tuple(f"scope.{index}" for index in range(subject._MAX_AUTHORIZED_SCOPES + 1)),
    )

    with pytest.raises(SourceRightsManifestError, match="maximum supported scope count"):
        authorize_source_use(
            current,
            source_identity=current.source_identity,
            required_scope="historical.read",
            at=NOW,
        )


def test_manifest_file_size_is_bounded_before_json_materialization(tmp_path: Path) -> None:
    path = tmp_path / "rights.json"
    path.write_bytes(b"{" + b"x" * subject._MAX_MANIFEST_BYTES)

    with pytest.raises(SourceRightsManifestError, match="maximum supported size"):
        load_source_rights_manifest(path)


def test_runtime_manifest_bytes_are_bounded_before_hash_or_json(tmp_path: Path) -> None:
    current = manifest(tmp_path)
    object.__setattr__(
        current,
        "manifest_bytes",
        b"x" * (subject._MAX_MANIFEST_BYTES + 1),
    )

    with pytest.raises(SourceRightsManifestError, match="maximum supported size"):
        authorize_source_use(
            current,
            source_identity=current.source_identity,
            required_scope="historical.read",
            at=NOW,
        )
