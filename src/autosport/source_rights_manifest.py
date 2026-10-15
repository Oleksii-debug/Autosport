from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any
from weakref import ReferenceType, ref


_MANIFEST_KIND = "autosport_source_rights_manifest"
_MAX_TEXT_LENGTH = 4096
_MAX_MANIFEST_BYTES = 64 * 1024
_MAX_AUTHORIZED_SCOPES = 256
_PRIVACY_CLASSIFICATIONS = frozenset(
    {"NON_PERSONAL_DATA", "PERSONAL_DATA_RESTRICTED", "UNKNOWN"}
)
_EVIDENCE_CLASSES = frozenset({"HUMAN_APPROVED_SOURCE_RIGHTS", "UNKNOWN"})
_REQUIRED_FIELDS = {
    "schema_version",
    "kind",
    "source_identity",
    "authorized_scopes",
    "privacy_classification",
    "evidence_class",
    "terms_reference",
    "retention_authority_reference",
    "effective_at",
    "expires_at",
    "human_approved",
    "approved_by",
    "approval_reference",
    "approved_at",
}
_AUTHORIZATION_ISSUER = object()


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
    privacy_classification: str
    evidence_class: str
    terms_reference: str
    retention_authority_reference: str
    effective_at: datetime
    expires_at: datetime
    approved_by: str
    approval_reference: str
    approved_at: datetime
    manifest_bytes: bytes = field(repr=False)


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class SourceRightsAuthorization:
    """Positive authorization result issued only by :func:`authorize_source_use`."""

    source_identity: str
    required_scope: str
    checked_at: datetime
    manifest_sha256: str
    privacy_classification: str
    evidence_class: str
    terms_reference: str
    retention_authority_reference: str
    approved_by: str
    approval_reference: str



def _install_source_rights_authorization_seal(
    cls: type[SourceRightsAuthorization],
    *,
    _issuer_token=_AUTHORIZATION_ISSUER,
    _error_type=SourceRightsManifestError,
    _ref_impl=ref,
    _id=id,
    _type=type,
) -> None:
    """Store positive authorization truth outside caller-mutable object slots."""

    field_names = (
        "source_identity",
        "required_scope",
        "checked_at",
        "manifest_sha256",
        "privacy_classification",
        "evidence_class",
        "terms_reference",
        "retention_authority_reference",
        "approved_by",
        "approval_reference",
    )
    issued: dict[
        int,
        tuple[ReferenceType[object], tuple[object, ...]],
    ] = {}
    lock = RLock()

    def _lookup(self: SourceRightsAuthorization) -> tuple[object, ...]:
        if _type(self) is not cls:
            raise _error_type(
                "source-rights authorization must be the exact canonical type"
            )
        with lock:
            record = issued.get(_id(self))
            if record is None or record[0]() is not self:
                raise _error_type(
                    "source-rights authorization is missing canonical issuance state"
                )
            return record[1]

    def _authorization_init(
        self: SourceRightsAuthorization,
        *,
        source_identity: str,
        required_scope: str,
        checked_at: datetime,
        manifest_sha256: str,
        privacy_classification: str,
        evidence_class: str,
        terms_reference: str,
        retention_authority_reference: str,
        approved_by: str,
        approval_reference: str,
        _issuer: object | None = None,
    ) -> None:
        if _type(self) is not cls or _issuer is not _issuer_token:
            raise _error_type(
                "SourceRightsAuthorization can only be issued by authorize_source_use"
            )
        key = _id(self)

        def forget(
            dead_ref: ReferenceType[object],
            *,
            issued_key: int = key,
        ) -> None:
            with lock:
                record = issued.get(issued_key)
                if record is not None and record[0] is dead_ref:
                    issued.pop(issued_key, None)

        state = (
            source_identity,
            required_scope,
            checked_at,
            manifest_sha256,
            privacy_classification,
            evidence_class,
            terms_reference,
            retention_authority_reference,
            approved_by,
            approval_reference,
        )
        authorization_ref = _ref_impl(self, forget)
        with lock:
            existing = issued.get(key)
            if existing is not None and existing[0]() is self:
                raise _error_type(
                    "source-rights authorization is already canonically issued"
                )
            issued[key] = (authorization_ref, state)

    def _field_property(index: int) -> property:
        def getter(
            self: SourceRightsAuthorization,
            *,
            _index=index,
            _lookup_impl=_lookup,
        ) -> object:
            return _lookup_impl(self)[_index]

        return property(getter)

    cls.__init__ = _authorization_init  # type: ignore[method-assign]
    for index, field_name in enumerate(field_names):
        setattr(cls, field_name, _field_property(index))


_install_source_rights_authorization_seal(SourceRightsAuthorization)
del _install_source_rights_authorization_seal


def _build_source_rights_authorization_issuer(
    *,
    _authorization_type=SourceRightsAuthorization,
    _issuer_token=_AUTHORIZATION_ISSUER,
):
    """Return the only product-owned positive authorization constructor."""

    def _issue_source_rights_authorization(
        *,
        source_identity: str,
        required_scope: str,
        checked_at: datetime,
        manifest_sha256: str,
        privacy_classification: str,
        evidence_class: str,
        terms_reference: str,
        retention_authority_reference: str,
        approved_by: str,
        approval_reference: str,
    ) -> SourceRightsAuthorization:
        return _authorization_type(
            source_identity=source_identity,
            required_scope=required_scope,
            checked_at=checked_at,
            manifest_sha256=manifest_sha256,
            privacy_classification=privacy_classification,
            evidence_class=evidence_class,
            terms_reference=terms_reference,
            retention_authority_reference=retention_authority_reference,
            approved_by=approved_by,
            approval_reference=approval_reference,
            _issuer=_issuer_token,
        )

    return _issue_source_rights_authorization


_issue_source_rights_authorization = _build_source_rights_authorization_issuer()
del _build_source_rights_authorization_issuer


def _strict_object(
    pairs: list[tuple[str, Any]],
    *,
    _error_type=SourceRightsManifestError,
    _dict_type=dict,
) -> dict[str, Any]:
    result: dict[str, Any] = _dict_type()
    for key, value in pairs:
        if key in result:
            raise _error_type(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_nonfinite(
    value: str,
    *,
    _error_type=SourceRightsManifestError,
) -> None:
    raise _error_type(f"non-finite JSON number: {value}")


def _canonical_text(
    value: object,
    *,
    field_name: str,
    _error_type=SourceRightsManifestError,
    _str_type=str,
    _max_text_length=_MAX_TEXT_LENGTH,
    _type=type,
) -> str:
    if (
        _type(value) is not _str_type
        or not value
        or len(value) > _max_text_length
    ):
        raise _error_type(
            f"{field_name} must be a non-empty canonical string without surrounding whitespace"
        )
    if value != value.strip():
        raise _error_type(
            f"{field_name} must be a non-empty canonical string without surrounding whitespace"
        )
    return value


def _classification(
    value: object,
    *,
    field_name: str,
    allowed: frozenset[str],
    _canonical_text_impl=_canonical_text,
    _error_type=SourceRightsManifestError,
) -> str:
    classification = _canonical_text_impl(value, field_name=field_name)
    if classification not in allowed:
        raise _error_type(
            f"{field_name} is unsupported; expected one of {sorted(allowed)}"
        )
    return classification


def _timestamp(
    value: object,
    *,
    field_name: str,
    _canonical_text_impl=_canonical_text,
    _datetime_type=datetime,
    _timezone_type=timezone,
    _timedelta_type=timedelta,
    _error_type=SourceRightsManifestError,
    _type=type,
) -> datetime:
    text = _canonical_text_impl(value, field_name=field_name)
    try:
        parsed = _datetime_type.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _error_type(
            f"{field_name} must be an ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None:
        raise _error_type(
            f"{field_name} must include an explicit timezone"
        )
    if (
        _type(parsed.tzinfo) is not _timezone_type
        or parsed.utcoffset() != _timedelta_type(0)
    ):
        raise _error_type(f"{field_name} must use UTC")
    return parsed


def _runtime_timestamp(
    value: object,
    *,
    field_name: str,
    _datetime_type=datetime,
    _timezone_type=timezone,
    _timedelta_type=timedelta,
    _error_type=SourceRightsManifestError,
    _type=type,
) -> datetime:
    if (
        _type(value) is not _datetime_type
        or _type(value.tzinfo) is not _timezone_type
    ):
        raise _error_type(f"{field_name} must be a built-in UTC datetime")
    if value.utcoffset() != _timedelta_type(0):
        raise _error_type(f"{field_name} must use UTC")
    return value


def _scopes(
    value: object,
    *,
    _canonical_text_impl=_canonical_text,
    _max_scopes=_MAX_AUTHORIZED_SCOPES,
    _error_type=SourceRightsManifestError,
    _list_type=list,
    _tuple_type=tuple,
    _set_type=set,
    _sorted=sorted,
    _type=type,
) -> tuple[str, ...]:
    if _type(value) is not _list_type or not value:
        raise _error_type("authorized_scopes must be a non-empty list")
    if len(value) > _max_scopes:
        raise _error_type(
            "authorized_scopes exceeds the maximum supported scope count"
        )
    normalized: list[str] = []
    for item in value:
        scope = _canonical_text_impl(
            item,
            field_name="authorized_scopes item",
        )
        if "*" in scope:
            raise _error_type(
                "authorized_scopes must use explicit scopes; wildcard scopes are forbidden"
            )
        normalized.append(scope)
    if len(_set_type(normalized)) != len(normalized):
        raise _error_type("authorized_scopes must not contain duplicates")
    return _tuple_type(_sorted(normalized))


def _runtime_scopes(
    value: object,
    *,
    _canonical_text_impl=_canonical_text,
    _max_scopes=_MAX_AUTHORIZED_SCOPES,
    _error_type=SourceRightsManifestError,
    _tuple_type=tuple,
    _set_type=set,
    _sorted=sorted,
    _type=type,
    _any=any,
) -> tuple[str, ...]:
    if _type(value) is not _tuple_type or not value:
        raise _error_type(
            "manifest authorized_scopes snapshot is malformed"
        )
    if len(value) > _max_scopes:
        raise _error_type(
            "manifest authorized_scopes snapshot exceeds the maximum supported scope count"
        )
    normalized = _tuple_type(
        _canonical_text_impl(
            item,
            field_name="manifest authorized_scopes item",
        )
        for item in value
    )
    if (
        normalized != _tuple_type(_sorted(normalized))
        or len(_set_type(normalized)) != len(normalized)
    ):
        raise _error_type(
            "manifest authorized_scopes snapshot is malformed"
        )
    if _any("*" in scope for scope in normalized):
        raise _error_type(
            "manifest authorized_scopes snapshot is malformed"
        )
    return normalized


def _bounded_manifest_bytes(
    value: object,
    *,
    _bytes_type=bytes,
    _max_manifest_bytes=_MAX_MANIFEST_BYTES,
    _error_type=SourceRightsManifestError,
    _type=type,
) -> bytes:
    if _type(value) is not _bytes_type:
        raise _error_type(
            "source-rights manifest snapshot bytes are malformed"
        )
    if len(value) > _max_manifest_bytes:
        raise _error_type(
            "source-rights manifest exceeds the maximum supported size"
        )
    return value


def _validated_projection(
    payload: bytes,
    *,
    _bounded_impl=_bounded_manifest_bytes,
    _json_loads=json.loads,
    _json_decode_error=json.JSONDecodeError,
    _strict_object_impl=_strict_object,
    _reject_nonfinite_impl=_reject_nonfinite,
    _required_fields=frozenset(_REQUIRED_FIELDS),
    _manifest_kind=_MANIFEST_KIND,
    _canonical_text_impl=_canonical_text,
    _scopes_impl=_scopes,
    _classification_impl=_classification,
    _privacy_classes=_PRIVACY_CLASSIFICATIONS,
    _evidence_classes=_EVIDENCE_CLASSES,
    _timestamp_impl=_timestamp,
    _error_type=SourceRightsManifestError,
    _dict_type=dict,
    _set_type=set,
    _int_type=int,
    _sorted=sorted,
    _type=type,
) -> tuple[str, tuple[str, ...], str, str, str, str, datetime, datetime, str, str, datetime]:
    payload = _bounded_impl(payload)
    try:
        raw = _json_loads(
            payload.decode("utf-8"),
            object_pairs_hook=_strict_object_impl,
            parse_constant=_reject_nonfinite_impl,
        )
    except UnicodeDecodeError as exc:
        raise _error_type(
            "source-rights manifest must be valid UTF-8 JSON"
        ) from exc
    except _json_decode_error as exc:
        raise _error_type(
            "source-rights manifest must be valid JSON"
        ) from exc
    except RecursionError as exc:
        raise _error_type(
            "source-rights manifest must be valid JSON"
        ) from exc

    if _type(raw) is not _dict_type:
        raise _error_type(
            "source-rights manifest must be a JSON object"
        )
    fields = _set_type(raw)
    if fields != _required_fields:
        missing = _sorted(_required_fields - fields)
        unknown = _sorted(fields - _required_fields)
        details: list[str] = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if unknown:
            details.append("unknown=" + ",".join(unknown))
        raise _error_type(
            "source-rights manifest fields must match schema exactly"
            + (": " + " ".join(details) if details else "")
        )

    if (
        _type(raw["schema_version"]) is not _int_type
        or raw["schema_version"] != 1
    ):
        raise _error_type(
            "source-rights manifest schema_version must be exact integer 1"
        )
    if raw["kind"] != _manifest_kind:
        raise _error_type(
            f"source-rights manifest kind must be {_manifest_kind}"
        )
    if raw["human_approved"] is not True:
        raise _error_type(
            "source-rights manifest must explicitly set human_approved=true"
        )

    source_identity = _canonical_text_impl(
        raw["source_identity"],
        field_name="source_identity",
    )
    authorized_scopes = _scopes_impl(raw["authorized_scopes"])
    privacy_classification = _classification_impl(
        raw["privacy_classification"],
        field_name="privacy_classification",
        allowed=_privacy_classes,
    )
    evidence_class = _classification_impl(
        raw["evidence_class"],
        field_name="evidence_class",
        allowed=_evidence_classes,
    )
    terms_reference = _canonical_text_impl(
        raw["terms_reference"],
        field_name="terms_reference",
    )
    retention_authority_reference = _canonical_text_impl(
        raw["retention_authority_reference"],
        field_name="retention_authority_reference",
    )
    effective_at = _timestamp_impl(
        raw["effective_at"],
        field_name="effective_at",
    )
    expires_at = _timestamp_impl(
        raw["expires_at"],
        field_name="expires_at",
    )
    approved_by = _canonical_text_impl(
        raw["approved_by"],
        field_name="approved_by",
    )
    approval_reference = _canonical_text_impl(
        raw["approval_reference"],
        field_name="approval_reference",
    )
    approved_at = _timestamp_impl(
        raw["approved_at"],
        field_name="approved_at",
    )

    if approved_at > effective_at:
        raise _error_type(
            "approved_at must not be later than effective_at"
        )
    if expires_at <= effective_at:
        raise _error_type(
            "expires_at must be later than effective_at"
        )

    return (
        source_identity,
        authorized_scopes,
        privacy_classification,
        evidence_class,
        terms_reference,
        retention_authority_reference,
        effective_at,
        expires_at,
        approved_by,
        approval_reference,
        approved_at,
    )


def _validated_manifest_snapshot(
    manifest: SourceRightsManifest,
    *,
    _bounded_impl=_bounded_manifest_bytes,
    _canonical_text_impl=_canonical_text,
    _runtime_scopes_impl=_runtime_scopes,
    _classification_impl=_classification,
    _privacy_classes=_PRIVACY_CLASSIFICATIONS,
    _evidence_classes=_EVIDENCE_CLASSES,
    _runtime_timestamp_impl=_runtime_timestamp,
    _error_type=SourceRightsManifestError,
) -> tuple[str, tuple[str, ...], str, str, str, str, datetime, datetime, str, str, datetime]:
    _bounded_impl(manifest.manifest_bytes)
    digest = _canonical_text_impl(
        manifest.manifest_sha256,
        field_name="manifest_sha256",
    )
    if (
        len(digest) != 64
        or any(ch not in "0123456789abcdef" for ch in digest)
    ):
        raise _error_type(
            "source-rights manifest snapshot digest is malformed"
        )
    return (
        _canonical_text_impl(
            manifest.source_identity,
            field_name="manifest source_identity",
        ),
        _runtime_scopes_impl(manifest.authorized_scopes),
        _classification_impl(
            manifest.privacy_classification,
            field_name="manifest privacy_classification",
            allowed=_privacy_classes,
        ),
        _classification_impl(
            manifest.evidence_class,
            field_name="manifest evidence_class",
            allowed=_evidence_classes,
        ),
        _canonical_text_impl(
            manifest.terms_reference,
            field_name="manifest terms_reference",
        ),
        _canonical_text_impl(
            manifest.retention_authority_reference,
            field_name="manifest retention_authority_reference",
        ),
        _runtime_timestamp_impl(
            manifest.effective_at,
            field_name="manifest effective_at",
        ),
        _runtime_timestamp_impl(
            manifest.expires_at,
            field_name="manifest expires_at",
        ),
        _canonical_text_impl(
            manifest.approved_by,
            field_name="manifest approved_by",
        ),
        _canonical_text_impl(
            manifest.approval_reference,
            field_name="manifest approval_reference",
        ),
        _runtime_timestamp_impl(
            manifest.approved_at,
            field_name="manifest approved_at",
        ),
    )


def _build_source_rights_loader(
    *,
    _path_type=Path,
    _max_manifest_bytes=_MAX_MANIFEST_BYTES,
    _bounded_impl=_bounded_manifest_bytes,
    _projection_impl=_validated_projection,
    _manifest_type=SourceRightsManifest,
    _sha256_constructor=hashlib.sha256,
    _error_type=SourceRightsManifestError,
    _str_type=str,
):
    """Compose the public loader from canonical policy-evidence roots."""

    def load_source_rights_manifest(
        path: str | Path,
    ) -> SourceRightsManifest:
        """Load and validate one exact source-rights manifest byte snapshot."""

        manifest_path = _path_type(path)
        try:
            with manifest_path.open("rb") as handle:
                payload = handle.read(_max_manifest_bytes + 1)
        except OSError as exc:
            raise _error_type(
                f"source-rights manifest is not readable: {manifest_path}"
            ) from exc
        payload = _bounded_impl(payload)

        (
            source_identity,
            authorized_scopes,
            privacy_classification,
            evidence_class,
            terms_reference,
            retention_authority_reference,
            effective_at,
            expires_at,
            approved_by,
            approval_reference,
            approved_at,
        ) = _projection_impl(payload)

        return _manifest_type(
            manifest_path=_str_type(manifest_path),
            manifest_sha256=_sha256_constructor(payload).hexdigest(),
            source_identity=source_identity,
            authorized_scopes=authorized_scopes,
            privacy_classification=privacy_classification,
            evidence_class=evidence_class,
            terms_reference=terms_reference,
            retention_authority_reference=retention_authority_reference,
            effective_at=effective_at,
            expires_at=expires_at,
            approved_by=approved_by,
            approval_reference=approval_reference,
            approved_at=approved_at,
            manifest_bytes=payload,
        )

    return load_source_rights_manifest


load_source_rights_manifest = _build_source_rights_loader()
del _build_source_rights_loader


def _build_authorize_source_use(
    *,
    _manifest_type=SourceRightsManifest,
    _canonical_text_impl=_canonical_text,
    _runtime_timestamp_impl=_runtime_timestamp,
    _snapshot_impl=_validated_manifest_snapshot,
    _projection_impl=_validated_projection,
    _sha256_constructor=hashlib.sha256,
    _issue_impl=_issue_source_rights_authorization,
    _error_type=SourceRightsManifestError,
    _type=type,
):
    """Compose positive source-use authorization from canonical roots only."""

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

        if _type(manifest) is not _manifest_type:
            raise _error_type(
                "manifest must be an exact verified SourceRightsManifest"
            )

        requested_source = _canonical_text_impl(
            source_identity,
            field_name="requested source_identity",
        )
        scope = _canonical_text_impl(
            required_scope,
            field_name="required_scope",
        )
        if "*" in scope:
            raise _error_type(
                "required_scope must be explicit; wildcard scopes are forbidden"
            )
        checked_at = _runtime_timestamp_impl(
            at,
            field_name="authorization check time",
        )

        object_projection = _snapshot_impl(manifest)
        if (
            _sha256_constructor(manifest.manifest_bytes).hexdigest()
            != manifest.manifest_sha256
        ):
            raise _error_type(
                "source-rights manifest snapshot digest is inconsistent"
            )

        projection = _projection_impl(manifest.manifest_bytes)
        if projection != object_projection:
            raise _error_type(
                "source-rights manifest snapshot fields are inconsistent"
            )

        if requested_source != manifest.source_identity:
            raise _error_type(
                "source_identity is not authorized by this manifest"
            )
        if manifest.privacy_classification != "NON_PERSONAL_DATA":
            raise _error_type(
                "privacy classification does not permit autonomous source use"
            )
        if manifest.evidence_class != "HUMAN_APPROVED_SOURCE_RIGHTS":
            raise _error_type(
                "evidence class does not support positive source-rights authorization"
            )
        if scope not in manifest.authorized_scopes:
            raise _error_type(
                "required_scope is not explicitly authorized by this manifest"
            )
        if checked_at < manifest.effective_at:
            raise _error_type(
                "source-rights authorization is not yet effective"
            )
        if checked_at >= manifest.expires_at:
            raise _error_type(
                "source-rights authorization has expired"
            )

        return _issue_impl(
            source_identity=manifest.source_identity,
            required_scope=scope,
            checked_at=checked_at,
            manifest_sha256=manifest.manifest_sha256,
            privacy_classification=manifest.privacy_classification,
            evidence_class=manifest.evidence_class,
            terms_reference=manifest.terms_reference,
            retention_authority_reference=manifest.retention_authority_reference,
            approved_by=manifest.approved_by,
            approval_reference=manifest.approval_reference,
        )

    return authorize_source_use


authorize_source_use = _build_authorize_source_use()
del _build_authorize_source_use

