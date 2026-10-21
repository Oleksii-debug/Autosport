"""Optional update identity for canonical Windows release BUILD_INFO.json.

Ordinary release packages remain unchanged. A package is eligible for later
signed-update admission only when both explicit canonical update identity
fields are authored and verified. This module does not sign or install.
"""

from __future__ import annotations

from .windows_update_admission import _schema, _version


class WindowsUpdateReleaseMetadataError(ValueError):
    """Release update identity is incomplete, ambiguous, or noncanonical."""



def validate_requested_update_identity(
    *, update_version: str | None = None, update_schema: int | None = None
) -> None:
    """Validate request before a release writer deletes/replaces output files."""
    if update_version is None and update_schema is None:
        return
    if update_version is None or update_schema is None:
        raise WindowsUpdateReleaseMetadataError("update identity requires both version and schema")
    try:
        _version(update_version)
        _schema(update_schema)
    except ValueError as exc:
        raise WindowsUpdateReleaseMetadataError("invalid canonical update identity") from exc


def attach_update_identity(
    build_info: dict[str, object],
    *,
    update_version: str | None = None,
    update_schema: int | None = None,
) -> dict[str, object]:
    """Return fresh BUILD_INFO fields; leave legacy releases byte-identical.

    ``version`` historically holds a prehuman display label. It is changed to
    canonical semantic version only for explicitly update-capable packages,
    because the signed envelope and embedded release identity must agree.
    The manifest schema_version is independent of the installed data schema.
    """
    if type(build_info) is not dict or build_info.get("product") != "Autosport":
        raise WindowsUpdateReleaseMetadataError("invalid release product identity")
    if type(build_info.get("version")) is not str:
        raise WindowsUpdateReleaseMetadataError("invalid release version field")
    if "update_version" in build_info or "update_schema" in build_info:
        raise WindowsUpdateReleaseMetadataError("release update fields must not be prepopulated")
    validate_requested_update_identity(
        update_version=update_version, update_schema=update_schema
    )
    if update_version is None:
        return dict(build_info)
    return {
        **build_info,
        "version": update_version,
        "update_version": update_version,
        "update_schema": update_schema,
    }


def validate_embedded_update_identity(build_info: dict[str, object]) -> None:
    """Fail closed on malformed optional fields in the canonical ZIP verifier.

    The caller must already have decoded BUILD_INFO using duplicate-key
    rejection and verified its manifest and digest. A missing pair is allowed
    for legacy release *inspection*, not for signed update admission.
    """
    if type(build_info) is not dict:
        raise WindowsUpdateReleaseMetadataError("invalid release metadata object")
    has_version = "update_version" in build_info
    has_schema = "update_schema" in build_info
    if has_version != has_schema:
        raise WindowsUpdateReleaseMetadataError("incomplete embedded update identity")
    if not has_version:
        return
    try:
        _version(build_info["update_version"])
        _schema(build_info["update_schema"])
    except ValueError as exc:
        raise WindowsUpdateReleaseMetadataError("invalid embedded update identity") from exc
    if type(build_info.get("version")) is not str or build_info["version"] != build_info["update_version"]:
        raise WindowsUpdateReleaseMetadataError("embedded update identity conflicts with release version")
