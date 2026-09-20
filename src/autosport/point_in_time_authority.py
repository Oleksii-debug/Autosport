from __future__ import annotations

"""Point-in-time research-data authority for Autosport.

This module does not create a second dataset or scientific registry. It binds
research/evaluation callers to the canonical DatasetSnapshot and FeatureSet
identities, then adds two missing fail-closed guarantees:

* feature/source revisions must have been causally available at the evaluated
  decision cutoff; and
* a confirmation/holdout window has one durable alias-resistant consumption
  identity and cannot silently become "fresh" again after restart.

The durable ledger reuses Autosport's canonical workspace writer lock, strict
JSON decoder and atomic JSON publication.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Mapping

from .integrity import atomic_write_json
from .json_integrity import strict_json_loads
from .scientific_registry import (
    DatasetSnapshot,
    FeatureSet,
    ScientificRegistry,
    promotion_holdout_access_id,
)
from .workspace_lock import WorkspaceEconomicLock


_HEX: Final = frozenset("0123456789abcdef")
_SOURCE_AUTHORITY_SCHEMA: Final = "autosport.point_in_time_source_authority"
_SOURCE_AUTHORITY_SCHEMA_VERSION: Final = 2
_SOURCE_AUTHORITY_ROOT_KEYS: Final = frozenset(
    {
        "schema",
        "schema_version",
        "policies",
        "witnesses",
        "feature_memberships",
        "revisions",
    }
)
_POLICY_KEYS: Final = frozenset(
    {
        "revision_policy_id",
        "source_identity",
        "policy_version",
        "policy_content_sha256",
        "policy_content_json",
        "witness_kind",
        "frozen_at",
        "record_sha256",
    }
)
_WITNESS_KEYS: Final = frozenset(
    {
        "availability_witness_id",
        "source_identity",
        "source_revision",
        "source_revision_sha256",
        "witness_kind",
        "witness_content_sha256",
        "witness_content_json",
        "source_as_of",
        "available_at",
        "recorded_at",
        "record_sha256",
    }
)
_FEATURE_MEMBERSHIP_KEYS: Final = frozenset(
    {
        "schema_version",
        "feature_membership_id",
        "feature_set_id",
        "feature_set_version",
        "feature_definition_sha256",
        "feature_manifest_json",
        "feature_source_sha256",
        "feature_name",
        "member_definition_sha256",
        "available_at",
        "record_sha256",
    }
)
_REVISION_KEYS: Final = frozenset(
    {
        "source_revision_authority_id",
        "source_identity",
        "source_revision",
        "source_revision_sha256",
        "revision_policy_id",
        "revision_policy_record_sha256",
        "availability_witness_id",
        "availability_witness_sha256",
        "availability_witness_record_sha256",
        "witness_kind",
        "source_as_of",
        "available_at",
        "recorded_at",
        "record_sha256",
    }
)
_ZERO_SHA256: Final = "0" * 64

_LEDGER_SCHEMA: Final = "autosport.holdout_consumption_ledger"
_LEDGER_SCHEMA_VERSION: Final = 2
_LEDGER_ROOT_KEYS: Final = frozenset(
    {
        "schema",
        "schema_version",
        "generation",
        "previous_ledger_sha256",
        "receipts",
        "ledger_sha256",
    }
)
_LEDGER_ANCHOR_SCHEMA: Final = "autosport.holdout_consumption_anchor"
_LEDGER_ANCHOR_SCHEMA_VERSION: Final = 1
_LEDGER_ANCHOR_KEYS: Final = frozenset(
    {"schema", "schema_version", "generation", "receipt_count", "ledger_sha256"}
)
_RECEIPT_KEYS: Final = frozenset(
    {
        "holdout_access_id",
        "dataset_snapshot_id",
        "dataset_manifest_sha256",
        "source_identity",
        "license_identity",
        "research_protocol_id",
        "confirmation_trial_family_id",
        "consumer_identity",
        "purpose",
        "consumed_at",
        "receipt_sha256",
    }
)


class PointInTimeAuthorityError(ValueError):
    """Base error for point-in-time evidence and holdout authority."""


class FeatureAvailabilityError(PointInTimeAuthorityError):
    """Feature/source evidence is not causally available at decision time."""


class SourceRevisionAuthorityError(PointInTimeAuthorityError):
    """Source revision or revision-policy authority is missing or inconsistent."""


class HoldoutConsumptionError(PointInTimeAuthorityError):
    """Holdout consumption evidence is malformed, unavailable or conflicting."""


class HoldoutAlreadyConsumedError(HoldoutConsumptionError):
    """The same semantic confirmation/holdout evidence was already consumed."""


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise PointInTimeAuthorityError(f"{name} must be a non-empty canonical string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise PointInTimeAuthorityError(f"{name} must contain valid UTF-8 text") from exc
    return value


def _sha256(value: object, name: str) -> str:
    text = _text(value, name).lower()
    if len(text) != 64 or any(character not in _HEX for character in text):
        raise PointInTimeAuthorityError(
            f"{name} must be a canonical SHA-256 hex string"
        )
    return text


def _aware_utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise PointInTimeAuthorityError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise PointInTimeAuthorityError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _instant(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PointInTimeAuthorityError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PointInTimeAuthorityError(f"{name} must include a timezone")
    return parsed.astimezone(UTC)


def _iso(value: datetime) -> str:
    normalized = _aware_utc(value, "timestamp")
    return normalized.isoformat().replace("+00:00", "Z")


def _canonical_json(payload: Mapping[str, object]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _require_exact_keys(
    name: str,
    payload: dict[str, object],
    expected: frozenset[str],
) -> None:
    keys = frozenset(payload)
    if keys != expected:
        missing = sorted(expected - keys)
        extra = sorted(keys - expected)
        raise HoldoutConsumptionError(
            f"{name} keys must match schema exactly; missing={missing!r} extra={extra!r}"
        )


@dataclass(frozen=True, slots=True)
class RevisionPolicyAuthority:
    """Frozen rule for interpreting one provider/source availability witness."""

    revision_policy_id: str
    source_identity: str
    policy_version: str
    policy_content_sha256: str
    policy_content_json: str
    witness_kind: str
    frozen_at: datetime

    @staticmethod
    def _parse_content(value: object) -> dict[str, object]:
        text = _text(value, "policy_content_json")
        try:
            raw = strict_json_loads(text)
        except (TypeError, ValueError) as exc:
            raise SourceRevisionAuthorityError(
                "revision policy content must be strict canonical JSON"
            ) from exc
        expected = frozenset(
            {
                "schema",
                "schema_version",
                "source_identity",
                "policy_version",
                "witness_kind",
                "availability_semantics",
            }
        )
        if not isinstance(raw, dict) or frozenset(raw) != expected:
            raise SourceRevisionAuthorityError(
                "revision policy content fields mismatch"
            )
        if (
            raw["schema"] != "autosport.revision_availability_policy"
            or raw["schema_version"] != 1
        ):
            raise SourceRevisionAuthorityError(
                "unsupported revision policy content schema"
            )
        canonical = {
            "schema": raw["schema"],
            "schema_version": raw["schema_version"],
            "source_identity": _text(
                raw["source_identity"],
                "policy source_identity",
            ),
            "policy_version": _text(
                raw["policy_version"],
                "policy_version",
            ),
            "witness_kind": _text(raw["witness_kind"], "witness_kind"),
            "availability_semantics": _text(
                raw["availability_semantics"],
                "availability_semantics",
            ),
        }
        if canonical["availability_semantics"] != "source_as_of<=available_at":
            raise SourceRevisionAuthorityError(
                "unsupported revision policy availability semantics"
            )
        if _canonical_json(canonical) != text:
            raise SourceRevisionAuthorityError(
                "revision policy content must use canonical encoding"
            )
        return canonical

    def __post_init__(self) -> None:
        for name in (
            "revision_policy_id",
            "source_identity",
            "policy_version",
            "witness_kind",
        ):
            _text(getattr(self, name), name)
        object.__setattr__(
            self,
            "policy_content_sha256",
            _sha256(self.policy_content_sha256, "policy_content_sha256"),
        )
        object.__setattr__(
            self,
            "policy_content_json",
            _text(self.policy_content_json, "policy_content_json"),
        )
        object.__setattr__(self, "frozen_at", _aware_utc(self.frozen_at, "frozen_at"))
        content = self._parse_content(self.policy_content_json)
        actual_content_sha256 = hashlib.sha256(
            self.policy_content_json.encode("utf-8")
        ).hexdigest()
        if actual_content_sha256 != self.policy_content_sha256:
            raise SourceRevisionAuthorityError(
                "revision policy content digest mismatch"
            )
        if (
            content["source_identity"] != self.source_identity
            or content["policy_version"] != self.policy_version
            or content["witness_kind"] != self.witness_kind
        ):
            raise SourceRevisionAuthorityError(
                "revision policy content does not match authority fields"
            )

    def to_payload(self) -> dict[str, object]:
        return {
            "revision_policy_id": self.revision_policy_id,
            "source_identity": self.source_identity,
            "policy_version": self.policy_version,
            "policy_content_sha256": self.policy_content_sha256,
            "policy_content_json": self.policy_content_json,
            "witness_kind": self.witness_kind,
            "frozen_at": _iso(self.frozen_at),
        }

    @property
    def authority_sha256(self) -> str:
        return _digest(self.to_payload())

    @classmethod
    def create(
        cls,
        *,
        revision_policy_id: str,
        policy_content_json: str,
        frozen_at: datetime,
    ) -> "RevisionPolicyAuthority":
        content = cls._parse_content(policy_content_json)
        return cls(
            revision_policy_id=revision_policy_id,
            source_identity=content["source_identity"],
            policy_version=content["policy_version"],
            policy_content_sha256=hashlib.sha256(
                policy_content_json.encode("utf-8")
            ).hexdigest(),
            policy_content_json=policy_content_json,
            witness_kind=content["witness_kind"],
            frozen_at=frozen_at,
        )

    @classmethod
    def from_payload(cls, payload: object) -> "RevisionPolicyAuthority":
        if not isinstance(payload, dict) or not all(
            isinstance(key, str) for key in payload
        ):
            raise SourceRevisionAuthorityError("revision policy must be a JSON object")
        body: dict[str, object] = payload
        keys = frozenset(body)
        if keys != _POLICY_KEYS:
            raise SourceRevisionAuthorityError("revision policy fields mismatch")
        record_sha256 = _sha256(body["record_sha256"], "record_sha256")
        policy = cls(
            revision_policy_id=body["revision_policy_id"],
            source_identity=body["source_identity"],
            policy_version=body["policy_version"],
            policy_content_sha256=body["policy_content_sha256"],
            policy_content_json=body["policy_content_json"],
            witness_kind=body["witness_kind"],
            frozen_at=_instant(body["frozen_at"], "frozen_at"),
        )
        if policy.authority_sha256 != record_sha256:
            raise SourceRevisionAuthorityError("revision policy record digest mismatch")
        return policy


@dataclass(frozen=True, slots=True)
class AvailabilityWitnessAuthority:
    """Immutable content-bearing witness for one source revision availability claim."""

    availability_witness_id: str
    source_identity: str
    source_revision: str
    source_revision_sha256: str
    witness_kind: str
    witness_content_sha256: str
    witness_content_json: str
    source_as_of: datetime
    available_at: datetime
    recorded_at: datetime

    @staticmethod
    def _parse_content(value: object) -> dict[str, object]:
        text = _text(value, "witness_content_json")
        try:
            raw = strict_json_loads(text)
        except (TypeError, ValueError) as exc:
            raise SourceRevisionAuthorityError(
                "availability witness content must be strict canonical JSON"
            ) from exc
        expected = frozenset(
            {
                "schema",
                "schema_version",
                "source_identity",
                "source_revision",
                "source_revision_sha256",
                "witness_kind",
                "source_as_of",
                "available_at",
            }
        )
        if not isinstance(raw, dict) or frozenset(raw) != expected:
            raise SourceRevisionAuthorityError(
                "availability witness content fields mismatch"
            )
        if (
            raw["schema"] != "autosport.source_availability_witness"
            or raw["schema_version"] != 1
        ):
            raise SourceRevisionAuthorityError(
                "unsupported availability witness content schema"
            )
        canonical = {
            "schema": raw["schema"],
            "schema_version": raw["schema_version"],
            "source_identity": _text(
                raw["source_identity"],
                "witness source_identity",
            ),
            "source_revision": _text(
                raw["source_revision"],
                "witness source_revision",
            ),
            "source_revision_sha256": _sha256(
                raw["source_revision_sha256"],
                "witness source_revision_sha256",
            ),
            "witness_kind": _text(raw["witness_kind"], "witness_kind"),
            "source_as_of": _iso(_instant(raw["source_as_of"], "source_as_of")),
            "available_at": _iso(_instant(raw["available_at"], "available_at")),
        }
        if _canonical_json(canonical) != text:
            raise SourceRevisionAuthorityError(
                "availability witness content must use canonical encoding"
            )
        return canonical

    def __post_init__(self) -> None:
        for name in (
            "availability_witness_id",
            "source_identity",
            "source_revision",
            "witness_kind",
        ):
            _text(getattr(self, name), name)
        for name in ("source_revision_sha256", "witness_content_sha256"):
            object.__setattr__(self, name, _sha256(getattr(self, name), name))
        object.__setattr__(
            self,
            "witness_content_json",
            _text(self.witness_content_json, "witness_content_json"),
        )
        for name in ("source_as_of", "available_at", "recorded_at"):
            object.__setattr__(self, name, _aware_utc(getattr(self, name), name))
        content = self._parse_content(self.witness_content_json)
        actual_content_sha256 = hashlib.sha256(
            self.witness_content_json.encode("utf-8")
        ).hexdigest()
        if actual_content_sha256 != self.witness_content_sha256:
            raise SourceRevisionAuthorityError(
                "availability witness content digest mismatch"
            )
        if (
            content["source_identity"] != self.source_identity
            or content["source_revision"] != self.source_revision
            or content["source_revision_sha256"] != self.source_revision_sha256
            or content["witness_kind"] != self.witness_kind
            or _instant(content["source_as_of"], "source_as_of") != self.source_as_of
            or _instant(content["available_at"], "available_at") != self.available_at
        ):
            raise SourceRevisionAuthorityError(
                "availability witness content does not match authority fields"
            )
        if self.source_as_of > self.available_at:
            raise SourceRevisionAuthorityError(
                "availability witness cannot precede its source as-of instant"
            )
        if self.available_at > self.recorded_at:
            raise SourceRevisionAuthorityError(
                "availability witness cannot be recorded before availability"
            )

    def to_payload(self) -> dict[str, object]:
        return {
            "availability_witness_id": self.availability_witness_id,
            "source_identity": self.source_identity,
            "source_revision": self.source_revision,
            "source_revision_sha256": self.source_revision_sha256,
            "witness_kind": self.witness_kind,
            "witness_content_sha256": self.witness_content_sha256,
            "witness_content_json": self.witness_content_json,
            "source_as_of": _iso(self.source_as_of),
            "available_at": _iso(self.available_at),
            "recorded_at": _iso(self.recorded_at),
        }

    @property
    def authority_sha256(self) -> str:
        return _digest(self.to_payload())

    @classmethod
    def create(
        cls,
        *,
        availability_witness_id: str,
        witness_content_json: str,
        recorded_at: datetime,
    ) -> "AvailabilityWitnessAuthority":
        content = cls._parse_content(witness_content_json)
        return cls(
            availability_witness_id=availability_witness_id,
            source_identity=content["source_identity"],
            source_revision=content["source_revision"],
            source_revision_sha256=content["source_revision_sha256"],
            witness_kind=content["witness_kind"],
            witness_content_sha256=hashlib.sha256(
                witness_content_json.encode("utf-8")
            ).hexdigest(),
            witness_content_json=witness_content_json,
            source_as_of=_instant(content["source_as_of"], "source_as_of"),
            available_at=_instant(content["available_at"], "available_at"),
            recorded_at=recorded_at,
        )

    @classmethod
    def from_payload(cls, payload: object) -> "AvailabilityWitnessAuthority":
        if not isinstance(payload, dict) or not all(
            isinstance(key, str) for key in payload
        ):
            raise SourceRevisionAuthorityError(
                "availability witness must be a JSON object"
            )
        body: dict[str, object] = payload
        if frozenset(body) != _WITNESS_KEYS:
            raise SourceRevisionAuthorityError("availability witness fields mismatch")
        record_sha256 = _sha256(body["record_sha256"], "record_sha256")
        witness = cls(
            availability_witness_id=body["availability_witness_id"],
            source_identity=body["source_identity"],
            source_revision=body["source_revision"],
            source_revision_sha256=body["source_revision_sha256"],
            witness_kind=body["witness_kind"],
            witness_content_sha256=body["witness_content_sha256"],
            witness_content_json=body["witness_content_json"],
            source_as_of=_instant(body["source_as_of"], "source_as_of"),
            available_at=_instant(body["available_at"], "available_at"),
            recorded_at=_instant(body["recorded_at"], "recorded_at"),
        )
        if witness.authority_sha256 != record_sha256:
            raise SourceRevisionAuthorityError(
                "availability witness record digest mismatch"
            )
        return witness


@dataclass(frozen=True, slots=True)
class FeatureMembershipAuthority:
    """Immutable binding of one named feature to one exact FeatureSet definition."""

    feature_membership_id: str
    feature_set_id: str
    feature_set_version: str
    feature_definition_sha256: str
    feature_manifest_json: str
    feature_source_sha256: str
    feature_name: str
    member_definition_sha256: str
    available_at: datetime

    @staticmethod
    def _member_from_manifest(
        *,
        feature_manifest_json: object,
        feature_definition_sha256: object,
        feature_name: object,
    ) -> str:
        manifest_text = _text(feature_manifest_json, "feature_manifest_json")
        definition_sha256 = _sha256(
            feature_definition_sha256,
            "feature_definition_sha256",
        )
        wanted_name = _text(feature_name, "feature_name")
        try:
            raw = strict_json_loads(manifest_text)
        except (TypeError, ValueError) as exc:
            raise SourceRevisionAuthorityError(
                "feature manifest must be strict canonical JSON"
            ) from exc
        if not isinstance(raw, dict) or frozenset(raw) != frozenset(
            {"schema", "schema_version", "features"}
        ):
            raise SourceRevisionAuthorityError(
                "feature manifest fields mismatch"
            )
        if (
            raw["schema"] != "autosport.feature_definition_manifest"
            or raw["schema_version"] != 1
        ):
            raise SourceRevisionAuthorityError(
                "unsupported feature manifest schema"
            )
        features = raw["features"]
        if not isinstance(features, dict) or not all(
            isinstance(name, str) and isinstance(value, str)
            for name, value in features.items()
        ):
            raise SourceRevisionAuthorityError(
                "feature manifest features must map names to SHA-256 digests"
            )
        canonical_features: dict[str, str] = {}
        for name, value in features.items():
            canonical_name = _text(name, "feature manifest name")
            canonical_digest = _sha256(value, "feature manifest member digest")
            if canonical_name != name or canonical_digest != value:
                raise SourceRevisionAuthorityError(
                    "feature manifest member entries must be canonical"
                )
            canonical_features[name] = value
        canonical_manifest = {
            "schema": raw["schema"],
            "schema_version": raw["schema_version"],
            "features": canonical_features,
        }
        if _canonical_json(canonical_manifest) != manifest_text:
            raise SourceRevisionAuthorityError(
                "feature manifest JSON must use canonical encoding"
            )
        actual_definition_sha256 = hashlib.sha256(
            manifest_text.encode("utf-8")
        ).hexdigest()
        if actual_definition_sha256 != definition_sha256:
            raise SourceRevisionAuthorityError(
                "feature manifest digest does not match FeatureSet definition"
            )
        member = canonical_features.get(wanted_name)
        if member is None:
            raise SourceRevisionAuthorityError(
                "feature is not present in canonical feature manifest"
            )
        return member

    def __post_init__(self) -> None:
        for name in ("feature_set_id", "feature_set_version", "feature_name"):
            _text(getattr(self, name), name)
        for name in (
            "feature_membership_id",
            "feature_definition_sha256",
            "feature_source_sha256",
            "member_definition_sha256",
        ):
            object.__setattr__(self, name, _sha256(getattr(self, name), name))
        object.__setattr__(
            self,
            "feature_manifest_json",
            _text(self.feature_manifest_json, "feature_manifest_json"),
        )
        object.__setattr__(
            self,
            "available_at",
            _aware_utc(self.available_at, "available_at"),
        )
        manifest_member = self._member_from_manifest(
            feature_manifest_json=self.feature_manifest_json,
            feature_definition_sha256=self.feature_definition_sha256,
            feature_name=self.feature_name,
        )
        if manifest_member != self.member_definition_sha256:
            raise SourceRevisionAuthorityError(
                "feature member digest does not match canonical feature manifest"
            )
        expected_id = _digest(self.semantic_payload())
        if self.feature_membership_id != expected_id:
            raise SourceRevisionAuthorityError(
                "feature membership identity does not match semantic binding"
            )

    def semantic_payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "feature_set_id": self.feature_set_id,
            "feature_set_version": self.feature_set_version,
            "feature_definition_sha256": self.feature_definition_sha256,
            "feature_manifest_json": self.feature_manifest_json,
            "feature_source_sha256": self.feature_source_sha256,
            "feature_name": self.feature_name,
            "member_definition_sha256": self.member_definition_sha256,
            "available_at": _iso(self.available_at),
        }

    def to_payload(self) -> dict[str, object]:
        return {
            "feature_membership_id": self.feature_membership_id,
            **self.semantic_payload(),
        }

    @property
    def authority_sha256(self) -> str:
        return _digest(self.to_payload())

    @classmethod
    def create(
        cls,
        *,
        feature_set_id: str,
        feature_set_version: str,
        feature_definition_sha256: str,
        feature_manifest_json: str,
        feature_source_sha256: str,
        feature_name: str,
        available_at: datetime,
    ) -> "FeatureMembershipAuthority":
        canonical_definition_sha256 = _sha256(
            feature_definition_sha256,
            "feature_definition_sha256",
        )
        canonical_manifest_json = _text(
            feature_manifest_json,
            "feature_manifest_json",
        )
        canonical_feature_name = _text(feature_name, "feature_name")
        member_definition_sha256 = cls._member_from_manifest(
            feature_manifest_json=canonical_manifest_json,
            feature_definition_sha256=canonical_definition_sha256,
            feature_name=canonical_feature_name,
        )
        semantic = {
            "schema_version": 1,
            "feature_set_id": _text(feature_set_id, "feature_set_id"),
            "feature_set_version": _text(feature_set_version, "feature_set_version"),
            "feature_definition_sha256": canonical_definition_sha256,
            "feature_manifest_json": canonical_manifest_json,
            "feature_source_sha256": _sha256(
                feature_source_sha256,
                "feature_source_sha256",
            ),
            "feature_name": canonical_feature_name,
            "member_definition_sha256": member_definition_sha256,
            "available_at": _iso(_aware_utc(available_at, "available_at")),
        }
        return cls(
            feature_membership_id=_digest(semantic),
            feature_set_id=semantic["feature_set_id"],
            feature_set_version=semantic["feature_set_version"],
            feature_definition_sha256=semantic["feature_definition_sha256"],
            feature_manifest_json=semantic["feature_manifest_json"],
            feature_source_sha256=semantic["feature_source_sha256"],
            feature_name=semantic["feature_name"],
            member_definition_sha256=semantic["member_definition_sha256"],
            available_at=_instant(semantic["available_at"], "available_at"),
        )

    @classmethod
    def from_payload(cls, payload: object) -> "FeatureMembershipAuthority":
        if not isinstance(payload, dict) or not all(
            isinstance(key, str) for key in payload
        ):
            raise SourceRevisionAuthorityError(
                "feature membership must be a JSON object"
            )
        body: dict[str, object] = payload
        if frozenset(body) != _FEATURE_MEMBERSHIP_KEYS:
            raise SourceRevisionAuthorityError("feature membership fields mismatch")
        if body["schema_version"] != 1:
            raise SourceRevisionAuthorityError(
                "unsupported feature membership schema_version"
            )
        record_sha256 = _sha256(body["record_sha256"], "record_sha256")
        membership = cls(
            feature_membership_id=body["feature_membership_id"],
            feature_set_id=body["feature_set_id"],
            feature_set_version=body["feature_set_version"],
            feature_definition_sha256=body["feature_definition_sha256"],
            feature_manifest_json=body["feature_manifest_json"],
            feature_source_sha256=body["feature_source_sha256"],
            feature_name=body["feature_name"],
            member_definition_sha256=body["member_definition_sha256"],
            available_at=_instant(body["available_at"], "available_at"),
        )
        if membership.authority_sha256 != record_sha256:
            raise SourceRevisionAuthorityError(
                "feature membership record digest mismatch"
            )
        return membership


@dataclass(frozen=True, slots=True)
class SourceRevisionAuthority:
    """Immutable authority binding one exact source revision to availability evidence."""

    source_revision_authority_id: str
    source_identity: str
    source_revision: str
    source_revision_sha256: str
    revision_policy_id: str
    revision_policy_record_sha256: str
    availability_witness_id: str
    availability_witness_sha256: str
    availability_witness_record_sha256: str
    witness_kind: str
    source_as_of: datetime
    available_at: datetime
    recorded_at: datetime

    def __post_init__(self) -> None:
        for name in (
            "source_revision_authority_id",
            "source_identity",
            "source_revision",
            "revision_policy_id",
            "availability_witness_id",
            "witness_kind",
        ):
            _text(getattr(self, name), name)
        for name in (
            "source_revision_sha256",
            "revision_policy_record_sha256",
            "availability_witness_sha256",
            "availability_witness_record_sha256",
        ):
            object.__setattr__(self, name, _sha256(getattr(self, name), name))
        for name in ("source_as_of", "available_at", "recorded_at"):
            object.__setattr__(self, name, _aware_utc(getattr(self, name), name))
        if self.source_as_of > self.available_at:
            raise SourceRevisionAuthorityError(
                "source revision cannot become available before its as-of instant"
            )
        if self.available_at > self.recorded_at:
            raise SourceRevisionAuthorityError(
                "source revision authority cannot be recorded before availability"
            )

    def to_payload(self) -> dict[str, object]:
        return {
            "source_revision_authority_id": self.source_revision_authority_id,
            "source_identity": self.source_identity,
            "source_revision": self.source_revision,
            "source_revision_sha256": self.source_revision_sha256,
            "revision_policy_id": self.revision_policy_id,
            "revision_policy_record_sha256": self.revision_policy_record_sha256,
            "availability_witness_id": self.availability_witness_id,
            "availability_witness_sha256": self.availability_witness_sha256,
            "availability_witness_record_sha256": self.availability_witness_record_sha256,
            "witness_kind": self.witness_kind,
            "source_as_of": _iso(self.source_as_of),
            "available_at": _iso(self.available_at),
            "recorded_at": _iso(self.recorded_at),
        }

    @property
    def authority_sha256(self) -> str:
        return _digest(self.to_payload())

    @classmethod
    def from_payload(cls, payload: object) -> "SourceRevisionAuthority":
        if not isinstance(payload, dict) or not all(
            isinstance(key, str) for key in payload
        ):
            raise SourceRevisionAuthorityError("source revision must be a JSON object")
        body: dict[str, object] = payload
        keys = frozenset(body)
        if keys != _REVISION_KEYS:
            raise SourceRevisionAuthorityError("source revision fields mismatch")
        record_sha256 = _sha256(body["record_sha256"], "record_sha256")
        revision = cls(
            source_revision_authority_id=body["source_revision_authority_id"],
            source_identity=body["source_identity"],
            source_revision=body["source_revision"],
            source_revision_sha256=body["source_revision_sha256"],
            revision_policy_id=body["revision_policy_id"],
            revision_policy_record_sha256=body["revision_policy_record_sha256"],
            availability_witness_id=body["availability_witness_id"],
            availability_witness_sha256=body["availability_witness_sha256"],
            availability_witness_record_sha256=body[
                "availability_witness_record_sha256"
            ],
            witness_kind=body["witness_kind"],
            source_as_of=_instant(body["source_as_of"], "source_as_of"),
            available_at=_instant(body["available_at"], "available_at"),
            recorded_at=_instant(body["recorded_at"], "recorded_at"),
        )
        if revision.authority_sha256 != record_sha256:
            raise SourceRevisionAuthorityError("source revision record digest mismatch")
        return revision


class SourceRevisionAuthorityStore:
    """Durable immutable resolver for source revisions and their frozen policies."""

    FILE_NAME: Final = "point_in_time_source_authority.json"

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace)
        self.path = self.workspace / self.FILE_NAME
        if not self.path.exists():
            raise SourceRevisionAuthorityError("source revision authority store is missing")
        self._read()

    @classmethod
    def initialize_pristine(
        cls,
        workspace: str | Path,
    ) -> "SourceRevisionAuthorityStore":
        root = Path(workspace)
        root.mkdir(parents=True, exist_ok=True)
        target = root / cls.FILE_NAME
        with WorkspaceEconomicLock(root):
            if not target.exists():
                atomic_write_json(
                    target,
                    {
                        "schema": _SOURCE_AUTHORITY_SCHEMA,
                        "schema_version": _SOURCE_AUTHORITY_SCHEMA_VERSION,
                        "policies": [],
                        "witnesses": [],
                        "feature_memberships": [],
                        "revisions": [],
                    },
                )
        return cls(root)

    @staticmethod
    def _stored_policy(policy: RevisionPolicyAuthority) -> dict[str, object]:
        payload = policy.to_payload()
        payload["record_sha256"] = policy.authority_sha256
        return payload

    @staticmethod
    def _stored_witness(
        witness: AvailabilityWitnessAuthority,
    ) -> dict[str, object]:
        payload = witness.to_payload()
        payload["record_sha256"] = witness.authority_sha256
        return payload

    @staticmethod
    def _stored_feature_membership(
        membership: FeatureMembershipAuthority,
    ) -> dict[str, object]:
        payload = membership.to_payload()
        payload["record_sha256"] = membership.authority_sha256
        return payload

    @staticmethod
    def _stored_revision(revision: SourceRevisionAuthority) -> dict[str, object]:
        payload = revision.to_payload()
        payload["record_sha256"] = revision.authority_sha256
        return payload

    def _read(self) -> dict[str, object]:
        try:
            raw = strict_json_loads(self.path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError) as exc:
            raise SourceRevisionAuthorityError(
                "invalid source revision authority store"
            ) from exc
        if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
            raise SourceRevisionAuthorityError(
                "source revision authority store must be a JSON object"
            )
        state: dict[str, object] = raw
        if frozenset(state) != _SOURCE_AUTHORITY_ROOT_KEYS:
            raise SourceRevisionAuthorityError(
                "source revision authority store fields mismatch"
            )
        if state["schema"] != _SOURCE_AUTHORITY_SCHEMA:
            raise SourceRevisionAuthorityError(
                "unsupported source revision authority schema"
            )
        if state["schema_version"] != _SOURCE_AUTHORITY_SCHEMA_VERSION:
            raise SourceRevisionAuthorityError(
                "unsupported source revision authority schema_version"
            )
        for field in ("policies", "witnesses", "feature_memberships", "revisions"):
            if not isinstance(state[field], list):
                raise SourceRevisionAuthorityError(
                    "source revision authority collections must be arrays"
                )
        policies = [
            RevisionPolicyAuthority.from_payload(item) for item in state["policies"]
        ]
        witnesses = [
            AvailabilityWitnessAuthority.from_payload(item)
            for item in state["witnesses"]
        ]
        feature_memberships = [
            FeatureMembershipAuthority.from_payload(item)
            for item in state["feature_memberships"]
        ]
        revisions = [
            SourceRevisionAuthority.from_payload(item) for item in state["revisions"]
        ]
        policy_ids: set[str] = set()
        for policy in policies:
            if policy.revision_policy_id in policy_ids:
                raise SourceRevisionAuthorityError(
                    "duplicate revision policy identity"
                )
            policy_ids.add(policy.revision_policy_id)
        witness_ids: set[str] = set()
        for witness in witnesses:
            if witness.availability_witness_id in witness_ids:
                raise SourceRevisionAuthorityError(
                    "duplicate availability witness identity"
                )
            witness_ids.add(witness.availability_witness_id)
        membership_ids: set[str] = set()
        membership_keys: set[tuple[str, str]] = set()
        for membership in feature_memberships:
            if membership.feature_membership_id in membership_ids:
                raise SourceRevisionAuthorityError(
                    "duplicate feature membership identity"
                )
            membership_ids.add(membership.feature_membership_id)
            key = (membership.feature_set_id, membership.feature_name)
            if key in membership_keys:
                raise SourceRevisionAuthorityError(
                    "conflicting feature membership authority"
                )
            membership_keys.add(key)
        revision_ids: set[str] = set()
        for revision in revisions:
            if revision.source_revision_authority_id in revision_ids:
                raise SourceRevisionAuthorityError(
                    "duplicate source revision authority identity"
                )
            revision_ids.add(revision.source_revision_authority_id)
        policy_by_id = {policy.revision_policy_id: policy for policy in policies}
        witness_by_id = {
            witness.availability_witness_id: witness for witness in witnesses
        }
        for revision in revisions:
            policy = policy_by_id.get(revision.revision_policy_id)
            if policy is None:
                raise SourceRevisionAuthorityError(
                    "source revision references unknown revision policy"
                )
            if revision.revision_policy_record_sha256 != policy.authority_sha256:
                raise SourceRevisionAuthorityError(
                    "source revision policy digest mismatch"
                )
            if revision.source_identity != policy.source_identity:
                raise SourceRevisionAuthorityError(
                    "source revision policy source identity mismatch"
                )
            if revision.witness_kind != policy.witness_kind:
                raise SourceRevisionAuthorityError(
                    "source revision witness kind is not authorized by policy"
                )
            if policy.frozen_at > revision.recorded_at:
                raise SourceRevisionAuthorityError(
                    "revision policy was not frozen before authority recording"
                )
            witness = witness_by_id.get(revision.availability_witness_id)
            if witness is None:
                raise SourceRevisionAuthorityError(
                    "source revision references unknown availability witness"
                )
            if revision.availability_witness_record_sha256 != witness.authority_sha256:
                raise SourceRevisionAuthorityError(
                    "source revision availability witness record digest mismatch"
                )
            if revision.availability_witness_sha256 != witness.witness_content_sha256:
                raise SourceRevisionAuthorityError(
                    "source revision availability witness content digest mismatch"
                )
            if (
                revision.source_identity != witness.source_identity
                or revision.source_revision != witness.source_revision
                or revision.source_revision_sha256 != witness.source_revision_sha256
                or revision.witness_kind != witness.witness_kind
            ):
                raise SourceRevisionAuthorityError(
                    "source revision availability witness identity mismatch"
                )
            if (
                revision.source_as_of != witness.source_as_of
                or revision.available_at != witness.available_at
            ):
                raise SourceRevisionAuthorityError(
                    "source revision availability witness time mismatch"
                )
            if witness.recorded_at > revision.recorded_at:
                raise SourceRevisionAuthorityError(
                    "source revision predates its availability witness record"
                )
        return state

    def register_policy(self, policy: RevisionPolicyAuthority) -> str:
        if not isinstance(policy, RevisionPolicyAuthority):
            raise SourceRevisionAuthorityError(
                "policy must be a RevisionPolicyAuthority"
            )
        with WorkspaceEconomicLock(self.workspace):
            state = self._read()
            stored = self._stored_policy(policy)
            for raw in state["policies"]:
                current = RevisionPolicyAuthority.from_payload(raw)
                if current.revision_policy_id != policy.revision_policy_id:
                    continue
                if current.authority_sha256 != policy.authority_sha256:
                    raise SourceRevisionAuthorityError(
                        "conflicting immutable revision policy identity"
                    )
                return current.authority_sha256
            policies = list(state["policies"])
            policies.append(stored)
            atomic_write_json(
                self.path,
                {
                    "schema": _SOURCE_AUTHORITY_SCHEMA,
                    "schema_version": _SOURCE_AUTHORITY_SCHEMA_VERSION,
                    "policies": policies,
                    "witnesses": list(state["witnesses"]),
                    "feature_memberships": list(state["feature_memberships"]),
                    "revisions": list(state["revisions"]),
                },
            )
        return policy.authority_sha256

    def resolve_policy(
        self,
        revision_policy_id: str,
        *,
        expected_sha256: str | None = None,
    ) -> RevisionPolicyAuthority:
        wanted = _text(revision_policy_id, "revision_policy_id")
        expected = (
            None
            if expected_sha256 is None
            else _sha256(expected_sha256, "expected_sha256")
        )
        state = self._read()
        for raw in state["policies"]:
            policy = RevisionPolicyAuthority.from_payload(raw)
            if policy.revision_policy_id == wanted:
                if expected is not None and policy.authority_sha256 != expected:
                    raise SourceRevisionAuthorityError(
                        "revision policy authority digest mismatch"
                    )
                return policy
        raise SourceRevisionAuthorityError("unknown revision policy authority")

    def register_witness(self, witness: AvailabilityWitnessAuthority) -> str:
        if not isinstance(witness, AvailabilityWitnessAuthority):
            raise SourceRevisionAuthorityError(
                "witness must be an AvailabilityWitnessAuthority"
            )
        with WorkspaceEconomicLock(self.workspace):
            state = self._read()
            stored = self._stored_witness(witness)
            for raw in state["witnesses"]:
                current = AvailabilityWitnessAuthority.from_payload(raw)
                if current.availability_witness_id != witness.availability_witness_id:
                    continue
                if current.authority_sha256 != witness.authority_sha256:
                    raise SourceRevisionAuthorityError(
                        "conflicting immutable availability witness identity"
                    )
                return current.authority_sha256
            witnesses = list(state["witnesses"])
            witnesses.append(stored)
            atomic_write_json(
                self.path,
                {
                    "schema": _SOURCE_AUTHORITY_SCHEMA,
                    "schema_version": _SOURCE_AUTHORITY_SCHEMA_VERSION,
                    "policies": list(state["policies"]),
                    "witnesses": witnesses,
                    "feature_memberships": list(state["feature_memberships"]),
                    "revisions": list(state["revisions"]),
                },
            )
        return witness.authority_sha256

    def resolve_witness(
        self,
        availability_witness_id: str,
        *,
        expected_sha256: str | None = None,
    ) -> AvailabilityWitnessAuthority:
        wanted = _text(availability_witness_id, "availability_witness_id")
        expected = (
            None
            if expected_sha256 is None
            else _sha256(expected_sha256, "expected_sha256")
        )
        state = self._read()
        for raw in state["witnesses"]:
            witness = AvailabilityWitnessAuthority.from_payload(raw)
            if witness.availability_witness_id == wanted:
                if expected is not None and witness.authority_sha256 != expected:
                    raise SourceRevisionAuthorityError(
                        "availability witness authority digest mismatch"
                    )
                return witness
        raise SourceRevisionAuthorityError("unknown availability witness authority")

    def register_feature_membership(
        self,
        membership: FeatureMembershipAuthority,
    ) -> str:
        if not isinstance(membership, FeatureMembershipAuthority):
            raise SourceRevisionAuthorityError(
                "membership must be a FeatureMembershipAuthority"
            )
        with WorkspaceEconomicLock(self.workspace):
            state = self._read()
            stored = self._stored_feature_membership(membership)
            for raw in state["feature_memberships"]:
                current = FeatureMembershipAuthority.from_payload(raw)
                if (
                    current.feature_set_id == membership.feature_set_id
                    and current.feature_name == membership.feature_name
                ):
                    if current.authority_sha256 != membership.authority_sha256:
                        raise SourceRevisionAuthorityError(
                            "conflicting immutable feature membership authority"
                        )
                    return current.authority_sha256
            memberships = list(state["feature_memberships"])
            memberships.append(stored)
            atomic_write_json(
                self.path,
                {
                    "schema": _SOURCE_AUTHORITY_SCHEMA,
                    "schema_version": _SOURCE_AUTHORITY_SCHEMA_VERSION,
                    "policies": list(state["policies"]),
                    "witnesses": list(state["witnesses"]),
                    "feature_memberships": memberships,
                    "revisions": list(state["revisions"]),
                },
            )
        return membership.authority_sha256

    def resolve_feature_membership(
        self,
        *,
        feature_set_id: str,
        feature_name: str,
    ) -> FeatureMembershipAuthority:
        wanted_set = _text(feature_set_id, "feature_set_id")
        wanted_name = _text(feature_name, "feature_name")
        state = self._read()
        matches = []
        for raw in state["feature_memberships"]:
            membership = FeatureMembershipAuthority.from_payload(raw)
            if (
                membership.feature_set_id == wanted_set
                and membership.feature_name == wanted_name
            ):
                matches.append(membership)
        if len(matches) != 1:
            raise SourceRevisionAuthorityError(
                "feature membership authority cannot be resolved uniquely"
            )
        return matches[0]

    def register_revision(self, revision: SourceRevisionAuthority) -> str:
        if not isinstance(revision, SourceRevisionAuthority):
            raise SourceRevisionAuthorityError(
                "revision must be a SourceRevisionAuthority"
            )
        with WorkspaceEconomicLock(self.workspace):
            state = self._read()
            policies = [
                RevisionPolicyAuthority.from_payload(item)
                for item in state["policies"]
            ]
            policy = next(
                (
                    item
                    for item in policies
                    if item.revision_policy_id == revision.revision_policy_id
                ),
                None,
            )
            if policy is None:
                raise SourceRevisionAuthorityError(
                    "source revision references unknown revision policy"
                )
            if revision.revision_policy_record_sha256 != policy.authority_sha256:
                raise SourceRevisionAuthorityError(
                    "source revision policy digest mismatch"
                )
            if revision.source_identity != policy.source_identity:
                raise SourceRevisionAuthorityError(
                    "source revision policy source identity mismatch"
                )
            if revision.witness_kind != policy.witness_kind:
                raise SourceRevisionAuthorityError(
                    "source revision witness kind is not authorized by policy"
                )
            if policy.frozen_at > revision.recorded_at:
                raise SourceRevisionAuthorityError(
                    "revision policy was not frozen before authority recording"
                )
            witnesses = [
                AvailabilityWitnessAuthority.from_payload(item)
                for item in state["witnesses"]
            ]
            witness = next(
                (
                    item
                    for item in witnesses
                    if item.availability_witness_id
                    == revision.availability_witness_id
                ),
                None,
            )
            if witness is None:
                raise SourceRevisionAuthorityError(
                    "source revision references unknown availability witness"
                )
            if revision.availability_witness_record_sha256 != witness.authority_sha256:
                raise SourceRevisionAuthorityError(
                    "source revision availability witness record digest mismatch"
                )
            if revision.availability_witness_sha256 != witness.witness_content_sha256:
                raise SourceRevisionAuthorityError(
                    "source revision availability witness content digest mismatch"
                )
            if (
                revision.source_identity != witness.source_identity
                or revision.source_revision != witness.source_revision
                or revision.source_revision_sha256 != witness.source_revision_sha256
                or revision.witness_kind != witness.witness_kind
            ):
                raise SourceRevisionAuthorityError(
                    "source revision availability witness identity mismatch"
                )
            if (
                revision.source_as_of != witness.source_as_of
                or revision.available_at != witness.available_at
            ):
                raise SourceRevisionAuthorityError(
                    "source revision availability witness time mismatch"
                )
            if witness.recorded_at > revision.recorded_at:
                raise SourceRevisionAuthorityError(
                    "source revision predates its availability witness record"
                )
            for raw in state["revisions"]:
                current = SourceRevisionAuthority.from_payload(raw)
                if (
                    current.source_revision_authority_id
                    != revision.source_revision_authority_id
                ):
                    continue
                if current.authority_sha256 != revision.authority_sha256:
                    raise SourceRevisionAuthorityError(
                        "conflicting immutable source revision authority identity"
                    )
                return current.authority_sha256
            revisions = list(state["revisions"])
            revisions.append(self._stored_revision(revision))
            atomic_write_json(
                self.path,
                {
                    "schema": _SOURCE_AUTHORITY_SCHEMA,
                    "schema_version": _SOURCE_AUTHORITY_SCHEMA_VERSION,
                    "policies": list(state["policies"]),
                    "witnesses": list(state["witnesses"]),
                    "feature_memberships": list(state["feature_memberships"]),
                    "revisions": revisions,
                },
            )
        return revision.authority_sha256

    def resolve_revision(
        self,
        source_revision_authority_id: str,
        *,
        expected_sha256: str | None = None,
    ) -> SourceRevisionAuthority:
        wanted = _text(
            source_revision_authority_id,
            "source_revision_authority_id",
        )
        expected = (
            None
            if expected_sha256 is None
            else _sha256(expected_sha256, "expected_sha256")
        )
        state = self._read()
        for raw in state["revisions"]:
            revision = SourceRevisionAuthority.from_payload(raw)
            if revision.source_revision_authority_id == wanted:
                if expected is not None and revision.authority_sha256 != expected:
                    raise SourceRevisionAuthorityError(
                        "source revision authority digest mismatch"
                    )
                return revision
        raise SourceRevisionAuthorityError("unknown source revision authority")


def _registry_record(
    scientific_registry: ScientificRegistry,
    record_type: str,
    record_id: str,
):
    if not isinstance(scientific_registry, ScientificRegistry):
        raise FeatureAvailabilityError(
            "scientific_registry must be a canonical ScientificRegistry"
        )
    try:
        entry = scientific_registry.get(record_type, record_id)
    except (OSError, TypeError, ValueError) as exc:
        raise FeatureAvailabilityError(
            f"cannot resolve canonical {record_type}"
        ) from exc
    if entry is None:
        raise FeatureAvailabilityError(
            f"canonical {record_type} record is missing"
        )
    return entry


def _require_registered_dataset(
    scientific_registry: ScientificRegistry,
    dataset_snapshot: DatasetSnapshot,
) -> None:
    if not isinstance(dataset_snapshot, DatasetSnapshot):
        raise HoldoutConsumptionError(
            "dataset_snapshot must be a canonical DatasetSnapshot"
        )
    try:
        entry = scientific_registry.get(
            "DatasetSnapshot",
            dataset_snapshot.dataset_snapshot_id,
        )
    except (OSError, TypeError, ValueError) as exc:
        raise HoldoutConsumptionError(
            "cannot resolve canonical DatasetSnapshot"
        ) from exc
    if entry is None or entry.payload != dataset_snapshot.to_payload():
        raise HoldoutConsumptionError(
            "dataset snapshot is not the exact registered canonical record"
        )


@dataclass(frozen=True, slots=True)
class FeatureAvailabilityEvidence:
    """Resolved proof that one registered feature/source revision was knowable in time."""

    dataset_snapshot_id: str
    dataset_record_sha256: str
    dataset_manifest_sha256: str
    source_identity: str
    license_identity: str
    dataset_causal_cutoff: datetime
    dataset_available_at: datetime
    feature_set_id: str
    feature_record_sha256: str
    feature_definition_sha256: str
    feature_source_sha256: str
    feature_set_available_at: datetime
    feature_membership_id: str
    feature_membership_sha256: str
    feature_member_definition_sha256: str
    feature_name: str
    source_revision_authority_id: str
    source_revision_authority_sha256: str
    source_revision: str
    source_revision_sha256: str
    revision_policy_id: str
    revision_policy_sha256: str
    availability_witness_id: str
    availability_witness_sha256: str
    availability_witness_record_sha256: str
    witness_kind: str
    source_as_of: datetime
    available_at: datetime
    decision_cutoff: datetime

    def __post_init__(self) -> None:
        for name in (
            "dataset_snapshot_id",
            "source_identity",
            "license_identity",
            "feature_set_id",
            "feature_name",
            "source_revision_authority_id",
            "source_revision",
            "revision_policy_id",
            "availability_witness_id",
            "witness_kind",
        ):
            _text(getattr(self, name), name)
        for name in (
            "dataset_record_sha256",
            "dataset_manifest_sha256",
            "feature_record_sha256",
            "feature_definition_sha256",
            "feature_source_sha256",
            "feature_membership_id",
            "feature_membership_sha256",
            "feature_member_definition_sha256",
            "source_revision_authority_sha256",
            "source_revision_sha256",
            "revision_policy_sha256",
            "availability_witness_sha256",
            "availability_witness_record_sha256",
        ):
            object.__setattr__(self, name, _sha256(getattr(self, name), name))
        for name in (
            "dataset_causal_cutoff",
            "dataset_available_at",
            "feature_set_available_at",
            "source_as_of",
            "available_at",
            "decision_cutoff",
        ):
            object.__setattr__(self, name, _aware_utc(getattr(self, name), name))
        if self.dataset_causal_cutoff > self.decision_cutoff:
            raise FeatureAvailabilityError(
                "dataset causal cutoff is after the evaluated decision cutoff"
            )
        if self.dataset_available_at > self.decision_cutoff:
            raise FeatureAvailabilityError(
                "dataset snapshot was not available at the evaluated decision cutoff"
            )
        if self.feature_set_available_at > self.decision_cutoff:
            raise FeatureAvailabilityError(
                "feature-set definition was not available at the evaluated decision cutoff"
            )
        if self.source_as_of > self.available_at:
            raise FeatureAvailabilityError(
                "source revision cannot become available before its as-of instant"
            )
        if self.available_at > self.decision_cutoff:
            raise FeatureAvailabilityError(
                "feature revision became available after the evaluated decision cutoff"
            )

    @classmethod
    def from_authorities(
        cls,
        *,
        scientific_registry: ScientificRegistry,
        source_authority_store: SourceRevisionAuthorityStore,
        dataset_snapshot_id: str,
        feature_set_id: str,
        feature_name: str,
        source_revision_authority_id: str,
        decision_cutoff: datetime,
    ) -> "FeatureAvailabilityEvidence":
        """Resolve every authority instead of accepting caller-shaped evidence."""

        if not isinstance(source_authority_store, SourceRevisionAuthorityStore):
            raise FeatureAvailabilityError(
                "source_authority_store must be a SourceRevisionAuthorityStore"
            )
        dataset_entry = _registry_record(
            scientific_registry,
            "DatasetSnapshot",
            _text(dataset_snapshot_id, "dataset_snapshot_id"),
        )
        feature_entry = _registry_record(
            scientific_registry,
            "FeatureSet",
            _text(feature_set_id, "feature_set_id"),
        )
        try:
            revision = source_authority_store.resolve_revision(
                source_revision_authority_id
            )
            policy = source_authority_store.resolve_policy(
                revision.revision_policy_id,
                expected_sha256=revision.revision_policy_record_sha256,
            )
            witness = source_authority_store.resolve_witness(
                revision.availability_witness_id,
                expected_sha256=revision.availability_witness_record_sha256,
            )
            membership = source_authority_store.resolve_feature_membership(
                feature_set_id=feature_entry.record_id,
                feature_name=feature_name,
            )
        except SourceRevisionAuthorityError as exc:
            raise FeatureAvailabilityError(
                "source revision or feature membership authority cannot be resolved"
            ) from exc

        dataset = dataset_entry.payload
        feature = feature_entry.payload
        if dataset.get("source_identity") != revision.source_identity:
            raise FeatureAvailabilityError(
                "source revision authority does not match dataset source identity"
            )
        if policy.source_identity != revision.source_identity:
            raise FeatureAvailabilityError(
                "revision policy does not match source revision identity"
            )
        if policy.witness_kind != revision.witness_kind:
            raise FeatureAvailabilityError(
                "revision policy does not authorize the availability witness kind"
            )
        if (
            witness.source_identity != revision.source_identity
            or witness.source_revision != revision.source_revision
            or witness.source_revision_sha256 != revision.source_revision_sha256
            or witness.witness_kind != revision.witness_kind
            or witness.witness_content_sha256 != revision.availability_witness_sha256
            or witness.source_as_of != revision.source_as_of
            or witness.available_at != revision.available_at
        ):
            raise FeatureAvailabilityError(
                "availability witness does not prove the resolved source revision"
            )
        feature_available_at = _instant(
            feature["available_at"],
            "FeatureSet.available_at",
        )
        if (
            membership.feature_set_version != feature["version"]
            or membership.feature_definition_sha256 != feature["definition_sha256"]
            or membership.feature_source_sha256 != feature["source_sha256"]
            or membership.available_at != feature_available_at
        ):
            raise FeatureAvailabilityError(
                "feature membership authority does not match registered FeatureSet"
            )
        return cls(
            dataset_snapshot_id=dataset_entry.record_id,
            dataset_record_sha256=dataset_entry.record_sha256,
            dataset_manifest_sha256=dataset["manifest_sha256"],
            source_identity=dataset["source_identity"],
            license_identity=dataset["license_identity"],
            dataset_causal_cutoff=_instant(
                dataset["causal_cutoff"],
                "DatasetSnapshot.causal_cutoff",
            ),
            dataset_available_at=_instant(
                dataset["available_at"],
                "DatasetSnapshot.available_at",
            ),
            feature_set_id=feature_entry.record_id,
            feature_record_sha256=feature_entry.record_sha256,
            feature_definition_sha256=feature["definition_sha256"],
            feature_source_sha256=feature["source_sha256"],
            feature_set_available_at=feature_available_at,
            feature_membership_id=membership.feature_membership_id,
            feature_membership_sha256=membership.authority_sha256,
            feature_member_definition_sha256=membership.member_definition_sha256,
            feature_name=membership.feature_name,
            source_revision_authority_id=revision.source_revision_authority_id,
            source_revision_authority_sha256=revision.authority_sha256,
            source_revision=revision.source_revision,
            source_revision_sha256=revision.source_revision_sha256,
            revision_policy_id=revision.revision_policy_id,
            revision_policy_sha256=policy.authority_sha256,
            availability_witness_id=revision.availability_witness_id,
            availability_witness_sha256=revision.availability_witness_sha256,
            availability_witness_record_sha256=witness.authority_sha256,
            witness_kind=revision.witness_kind,
            source_as_of=revision.source_as_of,
            available_at=revision.available_at,
            decision_cutoff=decision_cutoff,
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": "autosport.feature_availability_evidence",
            "schema_version": 3,
            "dataset_snapshot_id": self.dataset_snapshot_id,
            "dataset_record_sha256": self.dataset_record_sha256,
            "dataset_manifest_sha256": self.dataset_manifest_sha256,
            "source_identity": self.source_identity,
            "license_identity": self.license_identity,
            "dataset_causal_cutoff": _iso(self.dataset_causal_cutoff),
            "dataset_available_at": _iso(self.dataset_available_at),
            "feature_set_id": self.feature_set_id,
            "feature_record_sha256": self.feature_record_sha256,
            "feature_definition_sha256": self.feature_definition_sha256,
            "feature_source_sha256": self.feature_source_sha256,
            "feature_set_available_at": _iso(self.feature_set_available_at),
            "feature_membership_id": self.feature_membership_id,
            "feature_membership_sha256": self.feature_membership_sha256,
            "feature_member_definition_sha256": self.feature_member_definition_sha256,
            "feature_name": self.feature_name,
            "source_revision_authority_id": self.source_revision_authority_id,
            "source_revision_authority_sha256": self.source_revision_authority_sha256,
            "source_revision": self.source_revision,
            "source_revision_sha256": self.source_revision_sha256,
            "revision_policy_id": self.revision_policy_id,
            "revision_policy_sha256": self.revision_policy_sha256,
            "availability_witness_id": self.availability_witness_id,
            "availability_witness_sha256": self.availability_witness_sha256,
            "availability_witness_record_sha256": self.availability_witness_record_sha256,
            "witness_kind": self.witness_kind,
            "source_as_of": _iso(self.source_as_of),
            "available_at": _iso(self.available_at),
            "decision_cutoff": _iso(self.decision_cutoff),
        }

    @property
    def evidence_sha256(self) -> str:
        return _digest(self.to_payload())


@dataclass(frozen=True, slots=True)
class HoldoutConsumptionReceipt:
    """Immutable receipt for one semantic confirmation/holdout consumption."""

    holdout_access_id: str
    dataset_snapshot_id: str
    dataset_manifest_sha256: str
    source_identity: str
    license_identity: str
    research_protocol_id: str
    confirmation_trial_family_id: str
    consumer_identity: str
    purpose: str
    consumed_at: datetime
    receipt_sha256: str

    def __post_init__(self) -> None:
        for name in (
            "dataset_snapshot_id",
            "source_identity",
            "license_identity",
            "research_protocol_id",
            "confirmation_trial_family_id",
            "consumer_identity",
            "purpose",
        ):
            _text(getattr(self, name), name)
        object.__setattr__(
            self,
            "holdout_access_id",
            _sha256(self.holdout_access_id, "holdout_access_id"),
        )
        object.__setattr__(
            self,
            "dataset_manifest_sha256",
            _sha256(self.dataset_manifest_sha256, "dataset_manifest_sha256"),
        )
        object.__setattr__(
            self,
            "consumed_at",
            _aware_utc(self.consumed_at, "consumed_at"),
        )
        canonical_access_id = promotion_holdout_access_id(
            research_protocol_id=self.research_protocol_id,
            dataset_manifest_sha256=self.dataset_manifest_sha256,
            source_identity=self.source_identity,
            license_identity=self.license_identity,
            confirmation_trial_family_id=self.confirmation_trial_family_id,
        )
        if self.holdout_access_id != canonical_access_id:
            raise HoldoutConsumptionError(
                "holdout receipt semantic consumption identity mismatch"
            )
        expected = _digest(self.to_payload(include_digest=False))
        supplied = _sha256(self.receipt_sha256, "receipt_sha256")
        if supplied != expected:
            raise HoldoutConsumptionError("holdout receipt digest mismatch")
        object.__setattr__(self, "receipt_sha256", supplied)

    def to_payload(self, *, include_digest: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "holdout_access_id": self.holdout_access_id,
            "dataset_snapshot_id": self.dataset_snapshot_id,
            "dataset_manifest_sha256": self.dataset_manifest_sha256,
            "source_identity": self.source_identity,
            "license_identity": self.license_identity,
            "research_protocol_id": self.research_protocol_id,
            "confirmation_trial_family_id": self.confirmation_trial_family_id,
            "consumer_identity": self.consumer_identity,
            "purpose": self.purpose,
            "consumed_at": _iso(self.consumed_at),
        }
        if include_digest:
            payload["receipt_sha256"] = self.receipt_sha256
        return payload

    @classmethod
    def create(
        cls,
        *,
        holdout_access_id: str,
        dataset_snapshot: DatasetSnapshot,
        research_protocol_id: str,
        confirmation_trial_family_id: str,
        consumer_identity: str,
        purpose: str,
        consumed_at: datetime,
    ) -> "HoldoutConsumptionReceipt":
        consumed = _aware_utc(consumed_at, "consumed_at")
        base: dict[str, object] = {
            "holdout_access_id": _sha256(holdout_access_id, "holdout_access_id"),
            "dataset_snapshot_id": _text(
                dataset_snapshot.dataset_snapshot_id,
                "dataset_snapshot_id",
            ),
            "dataset_manifest_sha256": _sha256(
                dataset_snapshot.manifest_sha256,
                "dataset_manifest_sha256",
            ),
            "source_identity": _text(
                dataset_snapshot.source_identity,
                "source_identity",
            ),
            "license_identity": _text(
                dataset_snapshot.license_identity,
                "license_identity",
            ),
            "research_protocol_id": _text(
                research_protocol_id,
                "research_protocol_id",
            ),
            "confirmation_trial_family_id": _text(
                confirmation_trial_family_id,
                "confirmation_trial_family_id",
            ),
            "consumer_identity": _text(consumer_identity, "consumer_identity"),
            "purpose": _text(purpose, "purpose"),
            "consumed_at": _iso(consumed),
        }
        return cls(
            holdout_access_id=base["holdout_access_id"],
            dataset_snapshot_id=base["dataset_snapshot_id"],
            dataset_manifest_sha256=base["dataset_manifest_sha256"],
            source_identity=base["source_identity"],
            license_identity=base["license_identity"],
            research_protocol_id=base["research_protocol_id"],
            confirmation_trial_family_id=base["confirmation_trial_family_id"],
            consumer_identity=base["consumer_identity"],
            purpose=base["purpose"],
            consumed_at=consumed,
            receipt_sha256=_digest(base),
        )

    @classmethod
    def from_payload(cls, payload: object) -> "HoldoutConsumptionReceipt":
        if not isinstance(payload, dict) or not all(
            isinstance(key, str) for key in payload
        ):
            raise HoldoutConsumptionError("holdout receipt must be a JSON object")
        body: dict[str, object] = payload
        _require_exact_keys("holdout receipt", body, _RECEIPT_KEYS)
        return cls(
            holdout_access_id=body["holdout_access_id"],
            dataset_snapshot_id=body["dataset_snapshot_id"],
            dataset_manifest_sha256=body["dataset_manifest_sha256"],
            source_identity=body["source_identity"],
            license_identity=body["license_identity"],
            research_protocol_id=body["research_protocol_id"],
            confirmation_trial_family_id=body["confirmation_trial_family_id"],
            consumer_identity=body["consumer_identity"],
            purpose=body["purpose"],
            consumed_at=_instant(body["consumed_at"], "consumed_at"),
            receipt_sha256=body["receipt_sha256"],
        )


def holdout_identity(
    *,
    scientific_registry: ScientificRegistry,
    dataset_snapshot: DatasetSnapshot,
    research_protocol_id: str,
    confirmation_trial_family_id: str,
) -> str:
    """Return the canonical alias-resistant holdout access identity."""

    _require_registered_dataset(scientific_registry, dataset_snapshot)
    return promotion_holdout_access_id(
        research_protocol_id=research_protocol_id,
        dataset_manifest_sha256=dataset_snapshot.manifest_sha256,
        source_identity=dataset_snapshot.source_identity,
        license_identity=dataset_snapshot.license_identity,
        confirmation_trial_family_id=confirmation_trial_family_id,
    )


def _validate_holdout_available(
    dataset_snapshot: DatasetSnapshot,
    *,
    consumed_at: datetime,
) -> None:
    consumed = _aware_utc(consumed_at, "consumed_at")
    snapshot_available = _instant(
        dataset_snapshot.available_at_utc,
        "dataset_snapshot.available_at_utc",
    )
    causal_cutoff = _instant(
        dataset_snapshot.causal_cutoff,
        "dataset_snapshot.causal_cutoff",
    )
    if snapshot_available > consumed:
        raise HoldoutConsumptionError(
            "dataset snapshot was not available when holdout consumption was recorded"
        )
    if causal_cutoff > consumed:
        raise HoldoutConsumptionError(
            "dataset causal cutoff is after holdout consumption time"
        )
    if dataset_snapshot.outcome_reveal_after is None:
        raise HoldoutConsumptionError(
            "confirmation outcome reveal authority is missing"
        )
    reveal = _instant(
        dataset_snapshot.outcome_reveal_after,
        "dataset_snapshot.outcome_reveal_after",
    )
    if reveal > consumed:
        raise HoldoutConsumptionError(
            "confirmation outcome was not revealed at holdout consumption time"
        )


@dataclass(frozen=True, slots=True)
class _LedgerState:
    receipts: tuple[HoldoutConsumptionReceipt, ...]
    generation: int
    ledger_sha256: str


class HoldoutConsumptionLedger:
    """Restart-safe immutable ledger with an external current-head anchor."""

    FILE_NAME: Final = "holdout_consumption_ledger.json"
    ANCHOR_FILE_NAME: Final = "holdout_consumption_ledger.head.json"

    def __init__(
        self,
        workspace: str | Path,
        *,
        scientific_registry: ScientificRegistry,
    ) -> None:
        if not isinstance(scientific_registry, ScientificRegistry):
            raise HoldoutConsumptionError(
                "scientific_registry must be a canonical ScientificRegistry"
            )
        self.workspace = Path(workspace)
        self.path = self.workspace / self.FILE_NAME
        self.anchor_path = self.workspace / self.ANCHOR_FILE_NAME
        self.scientific_registry = scientific_registry

    @staticmethod
    def _ledger_body(
        receipts: tuple[HoldoutConsumptionReceipt, ...],
        *,
        generation: int,
        previous_ledger_sha256: str,
    ) -> dict[str, object]:
        return {
            "schema": _LEDGER_SCHEMA,
            "schema_version": _LEDGER_SCHEMA_VERSION,
            "generation": generation,
            "previous_ledger_sha256": previous_ledger_sha256,
            "receipts": [receipt.to_payload() for receipt in receipts],
        }

    @classmethod
    def _payload(
        cls,
        receipts: tuple[HoldoutConsumptionReceipt, ...],
        *,
        generation: int,
        previous_ledger_sha256: str,
    ) -> dict[str, object]:
        body = cls._ledger_body(
            receipts,
            generation=generation,
            previous_ledger_sha256=previous_ledger_sha256,
        )
        return {**body, "ledger_sha256": _digest(body)}

    @staticmethod
    def _anchor_payload(state: _LedgerState) -> dict[str, object]:
        return {
            "schema": _LEDGER_ANCHOR_SCHEMA,
            "schema_version": _LEDGER_ANCHOR_SCHEMA_VERSION,
            "generation": state.generation,
            "receipt_count": len(state.receipts),
            "ledger_sha256": state.ledger_sha256,
        }

    def _decode_ledger(self, text: str) -> _LedgerState:
        try:
            raw = strict_json_loads(text)
        except (TypeError, ValueError) as exc:
            raise HoldoutConsumptionError("invalid holdout ledger JSON") from exc
        if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
            raise HoldoutConsumptionError("holdout ledger must be a JSON object")
        root: dict[str, object] = raw
        _require_exact_keys("holdout ledger", root, _LEDGER_ROOT_KEYS)
        if root["schema"] != _LEDGER_SCHEMA:
            raise HoldoutConsumptionError("unsupported holdout ledger schema")
        version = root["schema_version"]
        if isinstance(version, bool) or not isinstance(version, int):
            raise HoldoutConsumptionError(
                "holdout ledger schema_version must be an integer"
            )
        if version != _LEDGER_SCHEMA_VERSION:
            raise HoldoutConsumptionError("unsupported holdout ledger schema_version")
        generation = root["generation"]
        if (
            isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation <= 0
        ):
            raise HoldoutConsumptionError(
                "holdout ledger generation must be a positive integer"
            )
        previous = _sha256(
            root["previous_ledger_sha256"],
            "previous_ledger_sha256",
        )
        raw_receipts = root["receipts"]
        if not isinstance(raw_receipts, list):
            raise HoldoutConsumptionError(
                "holdout ledger receipts must be a JSON array"
            )
        receipts = tuple(
            HoldoutConsumptionReceipt.from_payload(payload)
            for payload in raw_receipts
        )
        if generation != len(receipts):
            raise HoldoutConsumptionError(
                "holdout ledger generation/receipt count mismatch"
            )
        if generation == 1 and previous != _ZERO_SHA256:
            raise HoldoutConsumptionError(
                "first holdout ledger generation must start at zero predecessor"
            )
        seen: set[str] = set()
        for receipt in receipts:
            if receipt.holdout_access_id in seen:
                raise HoldoutConsumptionError(
                    "holdout ledger contains duplicate semantic consumption identity"
                )
            seen.add(receipt.holdout_access_id)
        supplied_digest = _sha256(root["ledger_sha256"], "ledger_sha256")
        expected_digest = _digest(
            self._ledger_body(
                receipts,
                generation=generation,
                previous_ledger_sha256=previous,
            )
        )
        if supplied_digest != expected_digest:
            raise HoldoutConsumptionError("holdout ledger root digest mismatch")
        return _LedgerState(
            receipts=receipts,
            generation=generation,
            ledger_sha256=supplied_digest,
        )

    def _decode_anchor(self, text: str) -> dict[str, object]:
        try:
            raw = strict_json_loads(text)
        except (TypeError, ValueError) as exc:
            raise HoldoutConsumptionError("invalid holdout ledger anchor JSON") from exc
        if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
            raise HoldoutConsumptionError(
                "holdout ledger anchor must be a JSON object"
            )
        anchor: dict[str, object] = raw
        _require_exact_keys("holdout ledger anchor", anchor, _LEDGER_ANCHOR_KEYS)
        if anchor["schema"] != _LEDGER_ANCHOR_SCHEMA:
            raise HoldoutConsumptionError("unsupported holdout ledger anchor schema")
        if anchor["schema_version"] != _LEDGER_ANCHOR_SCHEMA_VERSION:
            raise HoldoutConsumptionError(
                "unsupported holdout ledger anchor schema_version"
            )
        for field in ("generation", "receipt_count"):
            value = anchor[field]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise HoldoutConsumptionError(
                    f"holdout ledger anchor {field} must be a non-negative integer"
                )
        anchor["ledger_sha256"] = _sha256(
            anchor["ledger_sha256"],
            "anchor.ledger_sha256",
        )
        return anchor

    def _load_unlocked(self) -> _LedgerState:
        ledger_exists = self.path.exists()
        anchor_exists = self.anchor_path.exists()
        if not ledger_exists and not anchor_exists:
            return _LedgerState((), 0, _ZERO_SHA256)
        if ledger_exists != anchor_exists:
            raise HoldoutConsumptionError(
                "holdout ledger/anchor completeness mismatch"
            )
        try:
            ledger_text = self.path.read_text(encoding="utf-8")
            anchor_text = self.anchor_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise HoldoutConsumptionError(
                f"cannot read holdout ledger authority: {exc}"
            ) from exc
        state = self._decode_ledger(ledger_text)
        anchor = self._decode_anchor(anchor_text)
        if (
            anchor["generation"] != state.generation
            or anchor["receipt_count"] != len(state.receipts)
            or anchor["ledger_sha256"] != state.ledger_sha256
        ):
            raise HoldoutConsumptionError(
                "holdout ledger rollback/tail mismatch against current-head anchor"
            )
        return state

    def receipts(self) -> tuple[HoldoutConsumptionReceipt, ...]:
        return self._load_unlocked().receipts

    def receipt_for(self, holdout_access_id: str) -> HoldoutConsumptionReceipt | None:
        target = _sha256(holdout_access_id, "holdout_access_id")
        for receipt in self._load_unlocked().receipts:
            if receipt.holdout_access_id == target:
                return receipt
        return None

    def is_consumed(
        self,
        *,
        dataset_snapshot: DatasetSnapshot,
        research_protocol_id: str,
        confirmation_trial_family_id: str,
    ) -> bool:
        access_id = holdout_identity(
            scientific_registry=self.scientific_registry,
            dataset_snapshot=dataset_snapshot,
            research_protocol_id=research_protocol_id,
            confirmation_trial_family_id=confirmation_trial_family_id,
        )
        return self.receipt_for(access_id) is not None

    def consume(
        self,
        *,
        dataset_snapshot: DatasetSnapshot,
        research_protocol_id: str,
        confirmation_trial_family_id: str,
        consumer_identity: str,
        purpose: str,
        consumed_at: datetime,
    ) -> HoldoutConsumptionReceipt:
        """Atomically consume one registered semantic holdout identity exactly once."""

        _require_registered_dataset(self.scientific_registry, dataset_snapshot)
        _validate_holdout_available(dataset_snapshot, consumed_at=consumed_at)
        access_id = holdout_identity(
            scientific_registry=self.scientific_registry,
            dataset_snapshot=dataset_snapshot,
            research_protocol_id=research_protocol_id,
            confirmation_trial_family_id=confirmation_trial_family_id,
        )
        with WorkspaceEconomicLock(self.workspace):
            current = self._load_unlocked()
            for existing in current.receipts:
                if existing.holdout_access_id == access_id:
                    raise HoldoutAlreadyConsumedError(
                        "confirmation/holdout evidence was already consumed"
                    )
            receipt = HoldoutConsumptionReceipt.create(
                holdout_access_id=access_id,
                dataset_snapshot=dataset_snapshot,
                research_protocol_id=research_protocol_id,
                confirmation_trial_family_id=confirmation_trial_family_id,
                consumer_identity=consumer_identity,
                purpose=purpose,
                consumed_at=consumed_at,
            )
            next_state_payload = self._payload(
                current.receipts + (receipt,),
                generation=current.generation + 1,
                previous_ledger_sha256=current.ledger_sha256,
            )
            next_state = _LedgerState(
                receipts=current.receipts + (receipt,),
                generation=current.generation + 1,
                ledger_sha256=next_state_payload["ledger_sha256"],
            )
            atomic_write_json(self.path, next_state_payload)
            atomic_write_json(self.anchor_path, self._anchor_payload(next_state))
            verified = self._load_unlocked()
            if verified != next_state:
                raise HoldoutConsumptionError(
                    "published holdout ledger did not verify after write"
                )
            return receipt


__all__ = [
    "AvailabilityWitnessAuthority",
    "FeatureAvailabilityError",
    "FeatureMembershipAuthority",
    "FeatureAvailabilityEvidence",
    "HoldoutAlreadyConsumedError",
    "HoldoutConsumptionError",
    "HoldoutConsumptionLedger",
    "HoldoutConsumptionReceipt",
    "PointInTimeAuthorityError",
    "RevisionPolicyAuthority",
    "SourceRevisionAuthority",
    "SourceRevisionAuthorityError",
    "SourceRevisionAuthorityStore",
    "holdout_identity",
]
