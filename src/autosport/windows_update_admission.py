"""Fail-closed, side-effect-free admission for a future Windows update transaction.

This module never installs, deletes, downloads, or signs anything. It validates
version/schema continuity and a *separately verified* package identity before an
installer may even consider a candidate. A digest string is not a signature.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z", re.ASCII)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_MAX_VERSION_COMPONENT = 2**31 - 1
_MAX_SCHEMA = 2**31 - 1


class WindowsUpdateAdmissionError(ValueError):
    """Update cannot proceed without a valid and complete local authority."""


def _version(value: object) -> tuple[int, int, int]:
    if type(value) is not str or not _VERSION.fullmatch(value) or len(value) > 32:
        raise WindowsUpdateAdmissionError("invalid canonical three-part version")
    result = tuple(int(piece) for piece in value.split("."))
    if any(piece > _MAX_VERSION_COMPONENT for piece in result):
        raise WindowsUpdateAdmissionError("version component exceeds bound")
    return result  # type: ignore[return-value]


def _schema(value: object) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_SCHEMA:
        raise WindowsUpdateAdmissionError("invalid positive schema version")
    return value


def _sha(value: object) -> str:
    if type(value) is not str or not _SHA256.fullmatch(value):
        raise WindowsUpdateAdmissionError("invalid canonical SHA-256")
    return value


@dataclass(frozen=True, slots=True)
class InstalledWindowsProduct:
    version: str
    schema: int
    package_sha256: str

    def __post_init__(self) -> None:
        _version(self.version)
        _schema(self.schema)
        _sha(self.package_sha256)


@dataclass(frozen=True, slots=True)
class WindowsUpdateCandidate:
    version: str
    schema: int
    package_sha256: str
    verified_package_sha256: str
    # Only an independent trust verifier may set this flag. This is a
    # preflight boundary, not an implementation of cryptographic verification.
    signature_verified: bool

    def __post_init__(self) -> None:
        _version(self.version)
        _schema(self.schema)
        _sha(self.package_sha256)
        _sha(self.verified_package_sha256)
        if type(self.signature_verified) is not bool:
            raise WindowsUpdateAdmissionError("signature verification must be boolean")


@dataclass(frozen=True, slots=True)
class WindowsUpdateDecision:
    disposition: str  # NO_CHANGE or ELIGIBLE_FOR_TRANSACTION_PREPARE
    from_version: str
    to_version: str
    from_schema: int
    to_schema: int
    # Admission is not an installer capability or external money/credential authority.
    install_authorized: bool = False

    def __post_init__(self) -> None:
        # Preflight is never an installer permission. Reject forged truth labels
        # even when a caller directly constructs or dataclasses.replace()s us.
        if self.install_authorized is not False or type(self.install_authorized) is not bool:
            raise WindowsUpdateAdmissionError("preflight cannot authorize installation")
        # A decision is a portable receipt that may outlive its original
        # assessor. Do not let direct construction or frozen-object mutation
        # forge a downgrade, an unknown status or a no-op with changed state.
        old_version = _version(self.from_version)
        new_version = _version(self.to_version)
        old_schema = _schema(self.from_schema)
        new_schema = _schema(self.to_schema)
        if type(self.disposition) is not str:
            raise WindowsUpdateAdmissionError("noncanonical update disposition")
        if self.disposition == "NO_CHANGE":
            if new_version != old_version or new_schema != old_schema:
                raise WindowsUpdateAdmissionError("no-change decision changes installed state")
        elif self.disposition == "ELIGIBLE_FOR_TRANSACTION_PREPARE":
            if new_version <= old_version or new_schema < old_schema:
                raise WindowsUpdateAdmissionError("update decision is non-progressing or a downgrade")
        else:
            raise WindowsUpdateAdmissionError("noncanonical update disposition")


def assess_windows_update(
    installed: InstalledWindowsProduct,
    candidate: WindowsUpdateCandidate,
    *,
    supported_schema_migrations: tuple[tuple[int, int], ...],
) -> WindowsUpdateDecision:
    """Validate only update admission; does not authorize installation.

    Migration edges must be supplied by the actual installed updater. An edge
    is permitted only for a one-step forward schema migration; every step from
    installed.schema to candidate.schema must be explicitly supported.
    """
    if type(installed) is not InstalledWindowsProduct or type(candidate) is not WindowsUpdateCandidate:
        raise WindowsUpdateAdmissionError("update identities must be canonical product records")
    # Revalidate because an object can be forged via object.__new__/setattr__.
    installed.__post_init__()
    candidate.__post_init__()
    if not candidate.signature_verified:
        raise WindowsUpdateAdmissionError("independent package signature verification is required")
    if candidate.package_sha256 != candidate.verified_package_sha256:
        raise WindowsUpdateAdmissionError("package digest differs from independently verified bytes")
    old_version = _version(installed.version)
    new_version = _version(candidate.version)
    if new_version < old_version:
        raise WindowsUpdateAdmissionError("package version downgrade forbidden")
    if candidate.schema < installed.schema:
        raise WindowsUpdateAdmissionError("schema downgrade forbidden")
    if new_version == old_version:
        if candidate.schema != installed.schema or candidate.package_sha256 != installed.package_sha256:
            raise WindowsUpdateAdmissionError("same-version replacement or schema change forbidden")
        if type(supported_schema_migrations) is not tuple or supported_schema_migrations:
            raise WindowsUpdateAdmissionError("no-op must not carry migration evidence")
        return WindowsUpdateDecision("NO_CHANGE", installed.version, candidate.version, installed.schema, candidate.schema)
    # Migration evidence must be a fully materialized canonical chain. Never
    # execute a caller-controlled iterator while checking update authority;
    # generators may hang, mutate state, or hide a late invalid edge.
    if type(supported_schema_migrations) is not tuple:
        raise WindowsUpdateAdmissionError("migration evidence must be a canonical tuple")
    required_steps = candidate.schema - installed.schema
    if required_steps > 256 or len(supported_schema_migrations) != required_steps:
        raise WindowsUpdateAdmissionError("migration evidence must exactly match required chain")
    for offset, edge in enumerate(supported_schema_migrations):
        if type(edge) is not tuple or len(edge) != 2:
            raise WindowsUpdateAdmissionError("invalid migration edge")
        start, end = edge
        if type(start) is not int or type(end) is not int:
            raise WindowsUpdateAdmissionError("migration edge must contain exact integers")
        expected_start = installed.schema + offset
        if start != expected_start or end != expected_start + 1:
            raise WindowsUpdateAdmissionError("migration chain must be complete, ordered and gap-free")
    return WindowsUpdateDecision("ELIGIBLE_FOR_TRANSACTION_PREPARE", installed.version, candidate.version, installed.schema, candidate.schema)
