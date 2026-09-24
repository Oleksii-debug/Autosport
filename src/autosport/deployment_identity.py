"""Exact deployment/restart identity contract.

This module binds one runtime campaign generation to exact strategy, model, config,
and scientific-registry artifacts. It deliberately does not authorize deployment,
execution, provider access, release, or readiness. Persistence/recovery wiring lives
outside this contract.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final


_SCHEMA: Final = "autosport.deployment_restart_identity"
_SCHEMA_VERSION: Final = 1
_HEX: Final = frozenset("0123456789abcdef")
_RECORD_KEYS: Final = frozenset(
    {
        "schema",
        "schema_version",
        "deployment_id",
        "generation",
        "restart_sequence",
        "strategy_version_id",
        "strategy_artifact_sha256",
        "model_version_id",
        "model_artifact_sha256",
        "config_sha256",
        "scientific_registry_sha256",
        "created_at",
        "observed_at",
        "predecessor_fingerprint_sha256",
        "evidence_refs",
    }
)


class DeploymentIdentityError(ValueError):
    """Raised when deployment/restart identity evidence is non-canonical."""


def _canonical_text(name: str, value: object, *, maximum_bytes: int = 512) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise DeploymentIdentityError(f"{name} must be non-empty canonical text")
    if "\x00" in value:
        raise DeploymentIdentityError(f"{name} must not contain NUL")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise DeploymentIdentityError(f"{name} must be valid UTF-8 text") from exc
    if len(encoded) > maximum_bytes:
        raise DeploymentIdentityError(
            f"{name} must be at most {maximum_bytes} UTF-8 bytes"
        )
    return value


def _positive_int(name: str, value: object) -> int:
    if type(value) is not int or value <= 0:
        raise DeploymentIdentityError(
            f"{name} must be a positive non-boolean integer"
        )
    return value


def _nonnegative_int(name: str, value: object) -> int:
    if type(value) is not int or value < 0:
        raise DeploymentIdentityError(
            f"{name} must be a non-negative non-boolean integer"
        )
    return value


def _canonical_sha256(name: str, value: object) -> str:
    text = _canonical_text(name, value, maximum_bytes=64)
    if len(text) != 64 or any(character not in _HEX for character in text):
        raise DeploymentIdentityError(f"{name} must be lowercase SHA-256 hex")
    return text


def _canonical_utc(name: str, value: object) -> tuple[str, datetime]:
    text = _canonical_text(name, value, maximum_bytes=64)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise DeploymentIdentityError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DeploymentIdentityError(f"{name} must be timezone-aware")
    if parsed.utcoffset() != timedelta(0):
        raise DeploymentIdentityError(f"{name} must use UTC +00:00")
    if parsed.isoformat() != text:
        raise DeploymentIdentityError(
            f"{name} must use datetime.isoformat() canonical UTC form"
        )
    return text, parsed


def _canonical_refs(value: object) -> tuple[str, ...]:
    if type(value) is not tuple:
        raise DeploymentIdentityError("evidence_refs must be a tuple")
    refs = tuple(
        _canonical_text("evidence_ref", item, maximum_bytes=1024) for item in value
    )
    if not refs:
        raise DeploymentIdentityError("evidence_refs must not be empty")
    if refs != tuple(sorted(set(refs))):
        raise DeploymentIdentityError("evidence_refs must be sorted and unique")
    return refs


@dataclass(frozen=True, slots=True)
class DeploymentRestartIdentity:
    """Immutable identity evidence for one startup/restart observation."""

    deployment_id: str
    generation: int
    restart_sequence: int
    strategy_version_id: str
    strategy_artifact_sha256: str
    model_version_id: str
    model_artifact_sha256: str
    config_sha256: str
    scientific_registry_sha256: str
    created_at: str
    observed_at: str
    predecessor_fingerprint_sha256: str | None
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        _canonical_text("deployment_id", self.deployment_id, maximum_bytes=160)
        _positive_int("generation", self.generation)
        _nonnegative_int("restart_sequence", self.restart_sequence)
        _canonical_text(
            "strategy_version_id", self.strategy_version_id, maximum_bytes=256
        )
        _canonical_sha256("strategy_artifact_sha256", self.strategy_artifact_sha256)
        _canonical_text("model_version_id", self.model_version_id, maximum_bytes=256)
        _canonical_sha256("model_artifact_sha256", self.model_artifact_sha256)
        _canonical_sha256("config_sha256", self.config_sha256)
        _canonical_sha256("scientific_registry_sha256", self.scientific_registry_sha256)
        _, created = _canonical_utc("created_at", self.created_at)
        _, observed = _canonical_utc("observed_at", self.observed_at)
        if observed < created:
            raise DeploymentIdentityError("observed_at must not precede created_at")
        _canonical_refs(self.evidence_refs)

        predecessor = self.predecessor_fingerprint_sha256
        if self.restart_sequence == 0:
            if predecessor is not None:
                raise DeploymentIdentityError(
                    "initial deployment identity must not name a predecessor"
                )
            if observed != created:
                raise DeploymentIdentityError(
                    "initial deployment observed_at must equal created_at"
                )
        else:
            if predecessor is None:
                raise DeploymentIdentityError(
                    "restart identity requires predecessor_fingerprint_sha256"
                )
            _canonical_sha256("predecessor_fingerprint_sha256", predecessor)
            if observed <= created:
                raise DeploymentIdentityError(
                    "restart observed_at must be strictly after created_at"
                )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": _SCHEMA,
            "schema_version": _SCHEMA_VERSION,
            "deployment_id": self.deployment_id,
            "generation": self.generation,
            "restart_sequence": self.restart_sequence,
            "strategy_version_id": self.strategy_version_id,
            "strategy_artifact_sha256": self.strategy_artifact_sha256,
            "model_version_id": self.model_version_id,
            "model_artifact_sha256": self.model_artifact_sha256,
            "config_sha256": self.config_sha256,
            "scientific_registry_sha256": self.scientific_registry_sha256,
            "created_at": self.created_at,
            "observed_at": self.observed_at,
            "predecessor_fingerprint_sha256": self.predecessor_fingerprint_sha256,
            "evidence_refs": list(self.evidence_refs),
        }

    @classmethod
    def from_dict(cls, raw: object) -> "DeploymentRestartIdentity":
        if type(raw) is not dict or set(raw) != _RECORD_KEYS:
            raise DeploymentIdentityError(
                "deployment identity must contain exactly canonical fields"
            )
        if (
            type(raw["schema"]) is not str
            or raw["schema"] != _SCHEMA
            or type(raw["schema_version"]) is not int
            or raw["schema_version"] != _SCHEMA_VERSION
        ):
            raise DeploymentIdentityError("unsupported deployment identity schema")
        refs = raw["evidence_refs"]
        if type(refs) is not list or any(type(item) is not str for item in refs):
            raise DeploymentIdentityError("evidence_refs must be a JSON string array")
        predecessor = raw["predecessor_fingerprint_sha256"]
        if predecessor is not None and type(predecessor) is not str:
            raise DeploymentIdentityError(
                "predecessor_fingerprint_sha256 must be a JSON string or null"
            )
        return cls(
            deployment_id=raw["deployment_id"],
            generation=raw["generation"],
            restart_sequence=raw["restart_sequence"],
            strategy_version_id=raw["strategy_version_id"],
            strategy_artifact_sha256=raw["strategy_artifact_sha256"],
            model_version_id=raw["model_version_id"],
            model_artifact_sha256=raw["model_artifact_sha256"],
            config_sha256=raw["config_sha256"],
            scientific_registry_sha256=raw["scientific_registry_sha256"],
            created_at=raw["created_at"],
            observed_at=raw["observed_at"],
            predecessor_fingerprint_sha256=predecessor,
            evidence_refs=tuple(refs),
        )

    @property
    def fingerprint_sha256(self) -> str:
        encoded = json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


_IDENTITY_FIELDS: Final = (
    "deployment_id",
    "generation",
    "strategy_version_id",
    "strategy_artifact_sha256",
    "model_version_id",
    "model_artifact_sha256",
    "config_sha256",
    "scientific_registry_sha256",
    "created_at",
)


@dataclass(frozen=True, slots=True)
class RestartIdentityVerification:
    """Coherent restart-identity diagnostic; never standalone deployment authority."""

    expected_fingerprint_sha256: str
    observed_fingerprint_sha256: str
    expected_restart_sequence: int
    observed_restart_sequence: int
    accepted: bool
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        _canonical_sha256(
            "expected_fingerprint_sha256",
            self.expected_fingerprint_sha256,
        )
        _canonical_sha256(
            "observed_fingerprint_sha256",
            self.observed_fingerprint_sha256,
        )
        _positive_int("expected_restart_sequence", self.expected_restart_sequence)
        _nonnegative_int("observed_restart_sequence", self.observed_restart_sequence)
        if type(self.accepted) is not bool:
            raise DeploymentIdentityError("accepted must be a JSON boolean")
        if type(self.reasons) is not tuple:
            raise DeploymentIdentityError("reasons must be a tuple")
        reasons = tuple(
            _canonical_text("reason", reason, maximum_bytes=160)
            for reason in self.reasons
        )
        if reasons != tuple(dict.fromkeys(reasons)):
            raise DeploymentIdentityError("reasons must be unique")

        if self.accepted:
            if reasons:
                raise DeploymentIdentityError(
                    "accepted restart verification must not contain rejection reasons"
                )
            if self.observed_restart_sequence != self.expected_restart_sequence:
                raise DeploymentIdentityError(
                    "accepted restart verification requires the expected sequence"
                )
        elif not reasons:
            raise DeploymentIdentityError(
                "rejected restart verification requires at least one reason"
            )


def verify_restart_identity(
    previous: DeploymentRestartIdentity,
    observed: DeploymentRestartIdentity,
) -> RestartIdentityVerification:
    """Compare one restart observation against the immediately previous identity.

    All identity changes fail closed. A generation change is therefore not a restart;
    it needs a separate deployment/promotion authority outside this module.
    """

    if (
        type(previous) is not DeploymentRestartIdentity
        or type(observed) is not DeploymentRestartIdentity
    ):
        raise TypeError(
            "restart verification requires exact DeploymentRestartIdentity values"
        )

    reasons: list[str] = []
    for field in _IDENTITY_FIELDS:
        if getattr(previous, field) != getattr(observed, field):
            reasons.append(f"identity_mismatch:{field}")

    expected_sequence = previous.restart_sequence + 1
    if observed.restart_sequence != expected_sequence:
        reasons.append("restart_sequence_not_contiguous")
    if observed.predecessor_fingerprint_sha256 != previous.fingerprint_sha256:
        reasons.append("predecessor_fingerprint_mismatch")

    _, previous_observed = _canonical_utc("previous.observed_at", previous.observed_at)
    _, observed_at = _canonical_utc("observed.observed_at", observed.observed_at)
    if observed_at <= previous_observed:
        reasons.append("observed_at_not_strictly_forward")

    normalized = tuple(dict.fromkeys(reasons))
    return RestartIdentityVerification(
        expected_fingerprint_sha256=previous.fingerprint_sha256,
        observed_fingerprint_sha256=observed.fingerprint_sha256,
        expected_restart_sequence=expected_sequence,
        observed_restart_sequence=observed.restart_sequence,
        accepted=not normalized,
        reasons=normalized,
    )


def validate_restart_successor(
    previous: DeploymentRestartIdentity,
    observed: DeploymentRestartIdentity,
) -> None:
    """Raise when a restart observation is not an exact successor."""

    verification = verify_restart_identity(previous, observed)
    if not verification.accepted:
        raise DeploymentIdentityError(
            "restart identity rejected: " + ",".join(verification.reasons)
        )
