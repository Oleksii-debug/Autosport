"""Ed25519 detached-signature preflight for a pinned Windows update package.

Only a trusted public key supplied by the installed product is accepted. A
caller-supplied digest, unsigned manifest, or boolean flag is not signature
verification. This module does not install an update or authorize real money.

The signed statement binds exact package bytes, source commit, version and
schema. Keys and signatures are passed in; no private key is stored here.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .windows_update_admission import (
    InstalledWindowsProduct,
    WindowsUpdateAdmissionError,
    WindowsUpdateDecision,
    WindowsUpdateCandidate,
    _schema,
    _version,
    assess_windows_update,
)
from .windows_update_snapshot import (
    WindowsUpdateSnapshot,
    WindowsUpdateSnapshotError,
    verify_pinned_release_package,
)

_SOURCE_SHA = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)
_DOMAIN = b"AUTOSPORT-WINDOWS-UPDATE-ED25519-V1\n"


class WindowsUpdateSignatureError(ValueError):
    """Detached signature or signed update identity is not trustworthy."""


@dataclass(frozen=True, slots=True)
class VerifiedWindowsUpdate:
    decision: WindowsUpdateDecision
    package_sha256: str
    source_sha: str
    version: str
    schema: int
    install_authorized: bool = False

    def __post_init__(self) -> None:
        if self.install_authorized is not False or type(self.install_authorized) is not bool:
            raise WindowsUpdateSignatureError("signed preflight cannot authorize installation")
        # This object crosses the signature/installed-state/preflight boundary.
        # A success-shaped result with a mismatched package or decision must
        # not be accepted even if a future adapter returns a malformed record.
        if type(self.decision) is not WindowsUpdateDecision:
            raise WindowsUpdateSignatureError("signed preflight decision is not canonical")
        self.decision.__post_init__()
        if (
            type(self.package_sha256) is not str
            or re.fullmatch(r"[0-9a-f]{64}", self.package_sha256) is None
            or type(self.source_sha) is not str
            or _SOURCE_SHA.fullmatch(self.source_sha) is None
        ):
            raise WindowsUpdateSignatureError("signed preflight identity is not canonical")
        try:
            old_version = _version(self.decision.from_version)
            new_version = _version(self.version)
            old_schema = _schema(self.decision.from_schema)
            new_schema = _schema(self.schema)
        except WindowsUpdateAdmissionError:
            raise WindowsUpdateSignatureError("signed preflight version/schema is invalid") from None
        if (
            type(self.decision.to_version) is not str
            or self.decision.to_version != self.version
            or type(self.decision.to_schema) is not int
            or self.decision.to_schema != self.schema
            or new_version < old_version
            or new_schema < old_schema
        ):
            raise WindowsUpdateSignatureError("signed preflight decision conflicts with identity")
        if self.decision.disposition == "NO_CHANGE":
            if new_version != old_version or new_schema != old_schema:
                raise WindowsUpdateSignatureError("signed no-change decision is inconsistent")
        elif self.decision.disposition == "ELIGIBLE_FOR_TRANSACTION_PREPARE":
            if new_version <= old_version:
                raise WindowsUpdateSignatureError("signed update decision is inconsistent")
        else:
            raise WindowsUpdateSignatureError("signed preflight disposition is not canonical")


def _signed_statement(
    *, package_sha256: str, source_sha: str, version: str, schema: int
) -> bytes:
    # Exact base types prevent coercion or untrusted serialization hooks.
    if type(source_sha) is not str or not _SOURCE_SHA.fullmatch(source_sha):
        raise WindowsUpdateSignatureError("source commit must be canonical SHA-1")
    if type(package_sha256) is not str or not re.fullmatch(r"[0-9a-f]{64}", package_sha256):
        raise WindowsUpdateSignatureError("package digest must be canonical SHA-256")
    # The signer must use the *same* component bounds as the installed-state
    # assessor and release writer. A regex-only check accepted 2147483648.0.0
    # and produced signatures for releases that can never pass admission.
    try:
        _version(version)
    except WindowsUpdateAdmissionError:
        raise WindowsUpdateSignatureError("version must be canonical") from None
    try:
        _schema(schema)
    except WindowsUpdateAdmissionError:
        raise WindowsUpdateSignatureError("schema must be canonical") from None
    return _DOMAIN + json.dumps(
        {
            "package_sha256": package_sha256,
            "product": "Autosport",
            "schema": schema,
            "source_sha": source_sha,
            "version": version,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")



def _verify_signed_update_claim(
    *, package_sha256: str, source_sha: str, version: str, schema: int,
    signature: bytes, trusted_public_key: bytes,
) -> None:
    """Authenticate a claimed release envelope before expensive ZIP I/O.

    This is only a *cheap rejection* gate. A valid envelope authenticates the
    claimed SHA-256, not the bytes at any path. The later snapshot, canonical
    deep verifier and second Ed25519 check remain mandatory. No private keys
    are accepted or persisted by this verifier.
    """
    if type(signature) is not bytes or len(signature) != 64:
        raise WindowsUpdateSignatureError("invalid Ed25519 signature encoding")
    if type(trusted_public_key) is not bytes or len(trusted_public_key) != 32:
        raise WindowsUpdateSignatureError("invalid trusted Ed25519 public key")
    statement = _signed_statement(
        package_sha256=package_sha256, source_sha=source_sha,
        version=version, schema=schema,
    )
    try:
        from cryptography.exceptions import InvalidSignature, InternalError, UnsupportedAlgorithm
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except (ImportError, OSError, RuntimeError, SyntaxError):
        raise WindowsUpdateSignatureError("Ed25519 verification backend unavailable") from None
    try:
        Ed25519PublicKey.from_public_bytes(trusted_public_key).verify(signature, statement)
    except (UnsupportedAlgorithm, InternalError, OSError, RuntimeError):
        raise WindowsUpdateSignatureError("Ed25519 verification backend unavailable") from None
    except (InvalidSignature, ValueError):
        raise WindowsUpdateSignatureError("detached update signature rejected") from None


def assess_signed_windows_update(
    installed: InstalledWindowsProduct,
    snapshot: WindowsUpdateSnapshot,
    *,
    candidate_version: str,
    candidate_schema: int,
    expected_source_sha: str,
    signature: bytes,
    trusted_public_key: bytes,
    supported_schema_migrations: tuple[tuple[int, int], ...],
) -> VerifiedWindowsUpdate:
    """Verify signed package identity then assess downgrade/migration policy.

    The public key must come from a trusted, non-updatable installed authority.
    This routine is fail-closed when the cryptography backend is unavailable.
    The caller must keep the pinned snapshot context alive throughout preflight.
    """
    if type(snapshot) is not WindowsUpdateSnapshot:
        raise WindowsUpdateSignatureError("a pinned package snapshot is required")
    if type(signature) is not bytes or len(signature) != 64:
        raise WindowsUpdateSignatureError("invalid Ed25519 signature encoding")
    if type(trusted_public_key) is not bytes or len(trusted_public_key) != 32:
        raise WindowsUpdateSignatureError("invalid trusted Ed25519 public key")
    try:
        _signed_statement(
            package_sha256=snapshot.sha256,
            source_sha=expected_source_sha,
            version=candidate_version,
            schema=candidate_schema,
        )
        snapshot.recheck()
        # Direct callers may bypass the outer preflight entry point. Reject an
        # invalid signature before decompressing or deeply parsing the pinned
        # ZIP; then repeat the check after deep verification as a final gate.
        _verify_signed_update_claim(
            package_sha256=snapshot.sha256, source_sha=expected_source_sha,
            version=candidate_version, schema=candidate_schema,
            signature=signature, trusted_public_key=trusted_public_key,
        )
        report = verify_pinned_release_package(snapshot, expected_source_sha=expected_source_sha)
        # The signature binds a claimed version/schema, but an older payload
        # could otherwise be re-signed under a higher envelope version while
        # retaining its old embedded BUILD_INFO. The canonical deep package
        # verifier must attest the *embedded* update identity independently.
        # Legacy packages with no canonical update schema are not updatable.
        if (
            type(report) is not dict
            or type(report.get("status")) is not str
            or report["status"] != "PASS"
            or type(report.get("package_sha256")) is not str
            or report["package_sha256"] != snapshot.sha256
            or type(report.get("source_sha")) is not str
            or report["source_sha"] != expected_source_sha
        ):
            raise WindowsUpdateSignatureError("canonical package verifier evidence mismatch")
        if (
            type(report.get("update_version")) is not str
            or report["update_version"] != candidate_version
            or type(report.get("update_schema")) is not int
            or report["update_schema"] != candidate_schema
        ):
            raise WindowsUpdateSignatureError("embedded update identity not attested by package")
        # Reverify against the digest of the *pinned bytes* even though the
        # cheap preflight entry point may have authenticated the claim earlier.
        # This function is also a direct-call API and cannot rely on its caller.
        _verify_signed_update_claim(
            package_sha256=snapshot.sha256, source_sha=expected_source_sha,
            version=candidate_version, schema=candidate_schema,
            signature=signature, trusted_public_key=trusted_public_key,
        )
        snapshot.recheck()
        candidate = WindowsUpdateCandidate(
            version=candidate_version,
            schema=candidate_schema,
            package_sha256=snapshot.sha256,
            verified_package_sha256=snapshot.sha256,
            signature_verified=True,
        )
        decision = assess_windows_update(
            installed, candidate, supported_schema_migrations=supported_schema_migrations
        )
        snapshot.recheck()
    except (WindowsUpdateSnapshotError, WindowsUpdateAdmissionError):
        raise WindowsUpdateSignatureError("update integrity or policy admission failed") from None
    return VerifiedWindowsUpdate(
        decision=decision,
        package_sha256=snapshot.sha256,
        source_sha=expected_source_sha,
        version=candidate_version,
        schema=candidate_schema,
    )
