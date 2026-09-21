from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


_MANIFEST_KIND = "autosport_source_rights_manifest"
_REQUIRED_FIELDS = {
    "schema_version",
    "kind",
    "source_identity",
    "authorized_scopes",
    "effective_at",
    "expires_at",
    "human_approved",
    "approved_by",
    "approval_reference",
    "approved_at",
}


class SourceRightsManifestError(ValueError):
    """Raised when source-rights evidence is malformed or does not authorize a use."""


@dataclass(frozen=True, slots=True)
class SourceRightsManifest:
    """Verified immutable snapshot of a human-approved source-rights manifest.

    This record is a deterministic policy/evidence gate. It is not a legal opinion,
    does not prove provider entitlement by itself, and grants no provider-write,
    settlement, execution, or real-money authority.
    """

    manifest_path: str
    manifest_sha256: str
    source_identity: str
    authorized_scopes: tuple[str, ...]
    effective_at: datetime
    expires_at: datetime
    approved_by: str
    approval_reference: str
    approved_at: datetime
    manifest_bytes: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class SourceRightsAuthorization:
    source_identity: str
    required_scope: str
    checked_at: datetime
    manifest_sha256: str
    approved_by: str
    approval_reference: str


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SourceRightsManifestError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise SourceRightsManifestError(f"non-finite JSON number: {value}")


def _canonical_text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise SourceRightsManifestError(
            f"{field_name} must be a non-empty canonical string without surrounding whitespace"
        )
    return value


def _timestamp(value: object, *, field_name: str) -> datetime:
    text = _canonical_text(value, field_name=field_name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SourceRightsManifestError(
            f"{field_name} must be an ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SourceRightsManifestError(
            f"{field_name} must include an explicit timezone"
        )
    return parsed


def _scopes(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise SourceRightsManifestError(
            "authorized_scopes must be a non-empty list"
        )
    normalized: list[str] = []
    for item in value:
        scope = _canonical_text(item, field_name="authorized_scopes item")
        if "*" in scope:
            raise SourceRightsManifestError(
                "authorized_scopes must use explicit scopes; wildcard scopes are forbidden"
            )
        normalized.append(scope)
    if len(set(normalized)) != len(normalized):
        raise SourceRightsManifestError("authorized_scopes must not contain duplicates")
    return tuple(sorted(normalized))


def _validated_projection(
    payload: bytes,
) -> tuple[str, tuple[str, ...], datetime, datetime, str, str, datetime]:
    try:
        raw = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_nonfinite,
        )
    except UnicodeDecodeError as exc:
        raise SourceRightsManifestError(
            "source-rights manifest must be valid UTF-8 JSON"
        ) from exc
    except json.JSONDecodeError as exc:
        raise SourceRightsManifestError(
            "source-rights manifest must be valid JSON"
        ) from exc
    except RecursionError as exc:
        raise SourceRightsManifestError(
            "source-rights manifest must be valid JSON"
        ) from exc

    if not isinstance(raw, dict):
        raise SourceRightsManifestError("source-rights manifest must be a JSON object")
    fields = set(raw)
    if fields != _REQUIRED_FIELDS:
        missing = sorted(_REQUIRED_FIELDS - fields)
        unknown = sorted(fields - _REQUIRED_FIELDS)
        details: list[str] = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if unknown:
            details.append("unknown=" + ",".join(unknown))
        raise SourceRightsManifestError(
            "source-rights manifest fields must match schema exactly"
            + (": " + " ".join(details) if details else "")
        )

    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
        raise SourceRightsManifestError(
            "source-rights manifest schema_version must be exact integer 1"
        )
    if raw["kind"] != _MANIFEST_KIND:
        raise SourceRightsManifestError(
            f"source-rights manifest kind must be {_MANIFEST_KIND}"
        )
    if raw["human_approved"] is not True:
        raise SourceRightsManifestError(
            "source-rights manifest must explicitly set human_approved=true"
        )

    source_identity = _canonical_text(
        raw["source_identity"],
        field_name="source_identity",
    )
    authorized_scopes = _scopes(raw["authorized_scopes"])
    effective_at = _timestamp(raw["effective_at"], field_name="effective_at")
    expires_at = _timestamp(raw["expires_at"], field_name="expires_at")
    approved_by = _canonical_text(raw["approved_by"], field_name="approved_by")
    approval_reference = _canonical_text(
        raw["approval_reference"],
        field_name="approval_reference",
    )
    approved_at = _timestamp(raw["approved_at"], field_name="approved_at")

    if approved_at > effective_at:
        raise SourceRightsManifestError(
            "approved_at must not be later than effective_at"
        )
    if expires_at <= effective_at:
        raise SourceRightsManifestError(
            "expires_at must be later than effective_at"
        )

    return (
        source_identity,
        authorized_scopes,
        effective_at,
        expires_at,
        approved_by,
        approval_reference,
        approved_at,
    )


def load_source_rights_manifest(path: str | Path) -> SourceRightsManifest:
    """Load and validate one exact source-rights manifest byte snapshot."""

    manifest_path = Path(path)
    try:
        payload = manifest_path.read_bytes()
    except OSError as exc:
        raise SourceRightsManifestError(
            f"source-rights manifest is not readable: {manifest_path}"
        ) from exc

    (
        source_identity,
        authorized_scopes,
        effective_at,
        expires_at,
        approved_by,
        approval_reference,
        approved_at,
    ) = _validated_projection(payload)

    return SourceRightsManifest(
        manifest_path=str(manifest_path),
        manifest_sha256=hashlib.sha256(payload).hexdigest(),
        source_identity=source_identity,
        authorized_scopes=authorized_scopes,
        effective_at=effective_at,
        expires_at=expires_at,
        approved_by=approved_by,
        approval_reference=approval_reference,
        approved_at=approved_at,
        manifest_bytes=payload,
    )


def authorize_source_use(
    manifest: SourceRightsManifest,
    *,
    source_identity: str,
    required_scope: str,
    at: datetime,
) -> SourceRightsAuthorization:
    """Authorize one exact source/scope use at one instant.

    Scope matching is exact and case-sensitive. The valid interval is
    [effective_at, expires_at); equality with expires_at is expired.
    """

    if not isinstance(manifest, SourceRightsManifest):
        raise SourceRightsManifestError(
            "manifest must be a verified SourceRightsManifest"
        )

    requested_source = _canonical_text(
        source_identity,
        field_name="requested source_identity",
    )
    scope = _canonical_text(required_scope, field_name="required_scope")
    if "*" in scope:
        raise SourceRightsManifestError(
            "required_scope must be explicit; wildcard scopes are forbidden"
        )
    if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
        raise SourceRightsManifestError(
            "authorization check time must be timezone-aware"
        )

    if hashlib.sha256(manifest.manifest_bytes).hexdigest() != manifest.manifest_sha256:
        raise SourceRightsManifestError(
            "source-rights manifest snapshot digest is inconsistent"
        )

    projection = _validated_projection(manifest.manifest_bytes)
    object_projection = (
        manifest.source_identity,
        manifest.authorized_scopes,
        manifest.effective_at,
        manifest.expires_at,
        manifest.approved_by,
        manifest.approval_reference,
        manifest.approved_at,
    )
    if projection != object_projection:
        raise SourceRightsManifestError(
            "source-rights manifest snapshot fields are inconsistent"
        )

    if requested_source != manifest.source_identity:
        raise SourceRightsManifestError(
            "source_identity is not authorized by this manifest"
        )
    if scope not in manifest.authorized_scopes:
        raise SourceRightsManifestError(
            "required_scope is not explicitly authorized by this manifest"
        )
    if at < manifest.effective_at:
        raise SourceRightsManifestError(
            "source-rights authorization is not yet effective"
        )
    if at >= manifest.expires_at:
        raise SourceRightsManifestError(
            "source-rights authorization has expired"
        )

    return SourceRightsAuthorization(
        source_identity=manifest.source_identity,
        required_scope=scope,
        checked_at=at,
        manifest_sha256=manifest.manifest_sha256,
        approved_by=manifest.approved_by,
        approval_reference=manifest.approval_reference,
    )
