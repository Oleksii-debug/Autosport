"""Durable fail-closed registry for bookmaker technical and governance evidence.

The registry is read-model infrastructure only. It persists immutable technical capability
profiles and mechanically separate governance/terms evidence. It never grants execution
permission and never performs bookmaker network/write actions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile

from .bookmaker_capability import (
    BookmakerCapability,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
)


class BookmakerCapabilityRegistryError(ValueError):
    """Raised for corrupt or conflicting durable capability evidence."""


class GovernancePermissionState(str, Enum):
    """Recorded governance evidence state, separate from technical capability."""

    UNKNOWN = "unknown"
    PERMITTED = "permitted"
    PROHIBITED = "prohibited"


def _text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise BookmakerCapabilityRegistryError(
            f"{field} must be a non-empty trimmed string"
        )
    return value


def _timestamp(value: str, field: str) -> datetime:
    _text(value, field)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise BookmakerCapabilityRegistryError(
            f"{field} must be ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BookmakerCapabilityRegistryError(
            f"{field} must include a timezone offset"
        )
    return parsed


def _digest(value: str, field: str) -> str:
    _text(value, field)
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise BookmakerCapabilityRegistryError(
            f"{field} must be a lowercase 64-character SHA-256 hex digest"
        )
    return value


@dataclass(frozen=True, slots=True)
class BookmakerGovernanceEvidence:
    """Immutable terms/jurisdiction evidence; never execution authorization."""

    venue_id: str
    account_id: str
    jurisdiction: str
    terms_version: str
    automation_permission: GovernancePermissionState
    observed_at: str
    source_ref: str
    source_payload_sha256: str

    def __post_init__(self) -> None:
        _text(self.venue_id, "venue_id")
        _text(self.account_id, "account_id")
        _text(self.jurisdiction, "jurisdiction")
        _text(self.terms_version, "terms_version")
        if not isinstance(self.automation_permission, GovernancePermissionState):
            raise BookmakerCapabilityRegistryError(
                "automation_permission must be a GovernancePermissionState"
            )
        _timestamp(self.observed_at, "observed_at")
        _text(self.source_ref, "source_ref")
        _digest(self.source_payload_sha256, "source_payload_sha256")

    @property
    def evidence_id(self) -> str:
        encoded = json.dumps(
            self.to_canonical_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return sha256(encoded).hexdigest()

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "automation_permission": self.automation_permission.value,
            "jurisdiction": self.jurisdiction,
            "observed_at": self.observed_at,
            "source_payload_sha256": self.source_payload_sha256,
            "source_ref": self.source_ref,
            "terms_version": self.terms_version,
            "venue_id": self.venue_id,
        }


class BookmakerCapabilityRegistry:
    """Atomic durable history for technical profiles and governance evidence."""

    SCHEMA_VERSION = 1

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def register_profile(self, profile: BookmakerCapabilityProfile) -> bool:
        if not isinstance(profile, BookmakerCapabilityProfile):
            raise BookmakerCapabilityRegistryError(
                "profile must be a BookmakerCapabilityProfile"
            )
        document = self._load_document()
        profiles = self._profiles_from_document(document)
        profile_id = profile.profile_id
        if any(existing.profile_id == profile_id for existing in profiles):
            return False

        key = self._profile_key(profile)
        for existing in profiles:
            if self._profile_key(existing) == key:
                raise BookmakerCapabilityRegistryError(
                    "conflicting immutable profile identity for "
                    f"{profile.venue_id}/{profile.account_id}/"
                    f"{profile.adapter_id}/v{profile.profile_version}"
                )

        profiles.append(profile)
        self._write_document(
            profiles,
            self._governance_from_document(document),
        )
        return True

    def profile_history(
        self,
        venue_id: str,
        account_id: str,
        adapter_id: str,
    ) -> tuple[BookmakerCapabilityProfile, ...]:
        _text(venue_id, "venue_id")
        _text(account_id, "account_id")
        _text(adapter_id, "adapter_id")
        profiles = [
            profile
            for profile in self._profiles_from_document(self._load_document())
            if (
                profile.venue_id == venue_id
                and profile.account_id == account_id
                and profile.adapter_id == adapter_id
            )
        ]
        return tuple(
            sorted(
                profiles,
                key=lambda item: (
                    item.profile_version,
                    item.observed_at,
                    item.profile_id,
                ),
            )
        )

    def latest_profile(
        self,
        venue_id: str,
        account_id: str,
        adapter_id: str,
    ) -> BookmakerCapabilityProfile | None:
        history = self.profile_history(venue_id, account_id, adapter_id)
        return history[-1] if history else None

    def register_governance(
        self,
        evidence: BookmakerGovernanceEvidence,
    ) -> bool:
        if not isinstance(evidence, BookmakerGovernanceEvidence):
            raise BookmakerCapabilityRegistryError(
                "evidence must be BookmakerGovernanceEvidence"
            )
        document = self._load_document()
        governance = self._governance_from_document(document)
        evidence_id = evidence.evidence_id
        if any(existing.evidence_id == evidence_id for existing in governance):
            return False

        key = self._governance_key(evidence)
        for existing in governance:
            if self._governance_key(existing) == key:
                raise BookmakerCapabilityRegistryError(
                    "conflicting immutable governance evidence for "
                    f"{evidence.venue_id}/{evidence.account_id}/"
                    f"{evidence.jurisdiction}/{evidence.terms_version}"
                )

        governance.append(evidence)
        self._write_document(
            self._profiles_from_document(document),
            governance,
        )
        return True

    def governance_history(
        self,
        venue_id: str,
        account_id: str,
    ) -> tuple[BookmakerGovernanceEvidence, ...]:
        _text(venue_id, "venue_id")
        _text(account_id, "account_id")
        evidence = [
            item
            for item in self._governance_from_document(self._load_document())
            if item.venue_id == venue_id and item.account_id == account_id
        ]
        return tuple(
            sorted(
                evidence,
                key=lambda item: (
                    item.observed_at,
                    item.jurisdiction,
                    item.terms_version,
                    item.evidence_id,
                ),
            )
        )

    @staticmethod
    def _profile_key(
        profile: BookmakerCapabilityProfile,
    ) -> tuple[str, str, str, int]:
        return (
            profile.venue_id,
            profile.account_id,
            profile.adapter_id,
            profile.profile_version,
        )

    @staticmethod
    def _governance_key(
        evidence: BookmakerGovernanceEvidence,
    ) -> tuple[str, str, str, str, str]:
        return (
            evidence.venue_id,
            evidence.account_id,
            evidence.jurisdiction,
            evidence.terms_version,
            evidence.observed_at,
        )

    def _load_document(self) -> dict[str, object]:
        if not self.path.exists():
            return {
                "schema_version": self.SCHEMA_VERSION,
                "profiles": [],
                "governance": [],
            }
        try:
            raw = self.path.read_text(encoding="utf-8")
            document = json.loads(raw)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise BookmakerCapabilityRegistryError(
                "capability registry is unreadable or corrupt"
            ) from exc
        if not isinstance(document, dict):
            raise BookmakerCapabilityRegistryError(
                "capability registry root must be an object"
            )
        if document.get("schema_version") != self.SCHEMA_VERSION:
            raise BookmakerCapabilityRegistryError(
                "unsupported capability registry schema_version"
            )
        if not isinstance(document.get("profiles"), list):
            raise BookmakerCapabilityRegistryError("profiles must be a list")
        if not isinstance(document.get("governance"), list):
            raise BookmakerCapabilityRegistryError("governance must be a list")
        self._profiles_from_document(document)
        self._governance_from_document(document)
        return document

    def _profiles_from_document(
        self,
        document: dict[str, object],
    ) -> list[BookmakerCapabilityProfile]:
        raw_entries = document.get("profiles", [])
        if not isinstance(raw_entries, list):
            raise BookmakerCapabilityRegistryError("profiles must be a list")
        profiles: list[BookmakerCapabilityProfile] = []
        ids: set[str] = set()
        keys: dict[tuple[str, str, str, int], str] = {}
        for entry in raw_entries:
            if not isinstance(entry, dict):
                raise BookmakerCapabilityRegistryError(
                    "profile registry entry must be an object"
                )
            profile = self._decode_profile(entry.get("profile"))
            stored_id = entry.get("profile_id")
            if not isinstance(stored_id, str) or stored_id != profile.profile_id:
                raise BookmakerCapabilityRegistryError(
                    "stored profile_id does not match canonical profile identity"
                )
            if stored_id in ids:
                raise BookmakerCapabilityRegistryError(
                    "duplicate profile_id in capability registry"
                )
            key = self._profile_key(profile)
            previous = keys.get(key)
            if previous is not None and previous != stored_id:
                raise BookmakerCapabilityRegistryError(
                    "conflicting immutable profile identities in capability registry"
                )
            ids.add(stored_id)
            keys[key] = stored_id
            profiles.append(profile)
        return profiles

    def _governance_from_document(
        self,
        document: dict[str, object],
    ) -> list[BookmakerGovernanceEvidence]:
        raw_entries = document.get("governance", [])
        if not isinstance(raw_entries, list):
            raise BookmakerCapabilityRegistryError("governance must be a list")
        evidence_list: list[BookmakerGovernanceEvidence] = []
        ids: set[str] = set()
        keys: dict[tuple[str, str, str, str, str], str] = {}
        for entry in raw_entries:
            if not isinstance(entry, dict):
                raise BookmakerCapabilityRegistryError(
                    "governance registry entry must be an object"
                )
            evidence = self._decode_governance(entry.get("evidence"))
            stored_id = entry.get("evidence_id")
            if not isinstance(stored_id, str) or stored_id != evidence.evidence_id:
                raise BookmakerCapabilityRegistryError(
                    "stored evidence_id does not match canonical governance identity"
                )
            if stored_id in ids:
                raise BookmakerCapabilityRegistryError(
                    "duplicate evidence_id in capability registry"
                )
            key = self._governance_key(evidence)
            previous = keys.get(key)
            if previous is not None and previous != stored_id:
                raise BookmakerCapabilityRegistryError(
                    "conflicting immutable governance identities in registry"
                )
            ids.add(stored_id)
            keys[key] = stored_id
            evidence_list.append(evidence)
        return evidence_list

    @staticmethod
    def _decode_profile(raw: object) -> BookmakerCapabilityProfile:
        if not isinstance(raw, dict):
            raise BookmakerCapabilityRegistryError(
                "profile payload must be an object"
            )
        try:
            raw_facts = raw["facts"]
            if not isinstance(raw_facts, list):
                raise TypeError("facts")
            facts = tuple(
                BookmakerCapabilityFact(
                    capability=BookmakerCapability(item["capability"]),
                    state=BookmakerCapabilityState(item["state"]),
                )
                for item in raw_facts
                if isinstance(item, dict)
            )
            if len(facts) != len(raw_facts):
                raise TypeError("facts")
            return BookmakerCapabilityProfile(
                venue_id=raw["venue_id"],
                account_id=raw["account_id"],
                adapter_id=raw["adapter_id"],
                adapter_version=raw["adapter_version"],
                profile_version=raw["profile_version"],
                facts=facts,
                observed_at=raw["observed_at"],
                source_ref=raw["source_ref"],
                source_payload_sha256=raw["source_payload_sha256"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise BookmakerCapabilityRegistryError(
                "invalid capability profile payload"
            ) from exc

    @staticmethod
    def _decode_governance(raw: object) -> BookmakerGovernanceEvidence:
        if not isinstance(raw, dict):
            raise BookmakerCapabilityRegistryError(
                "governance payload must be an object"
            )
        try:
            return BookmakerGovernanceEvidence(
                venue_id=raw["venue_id"],
                account_id=raw["account_id"],
                jurisdiction=raw["jurisdiction"],
                terms_version=raw["terms_version"],
                automation_permission=GovernancePermissionState(
                    raw["automation_permission"]
                ),
                observed_at=raw["observed_at"],
                source_ref=raw["source_ref"],
                source_payload_sha256=raw["source_payload_sha256"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise BookmakerCapabilityRegistryError(
                "invalid governance evidence payload"
            ) from exc

    def _write_document(
        self,
        profiles: list[BookmakerCapabilityProfile],
        governance: list[BookmakerGovernanceEvidence],
    ) -> None:
        document = {
            "schema_version": self.SCHEMA_VERSION,
            "profiles": [
                {
                    "profile_id": profile.profile_id,
                    "profile": profile.to_canonical_dict(),
                }
                for profile in sorted(
                    profiles,
                    key=lambda item: (
                        item.venue_id,
                        item.account_id,
                        item.adapter_id,
                        item.profile_version,
                        item.profile_id,
                    ),
                )
            ],
            "governance": [
                {
                    "evidence_id": evidence.evidence_id,
                    "evidence": evidence.to_canonical_dict(),
                }
                for evidence in sorted(
                    governance,
                    key=lambda item: (
                        item.venue_id,
                        item.account_id,
                        item.observed_at,
                        item.evidence_id,
                    ),
                )
            ],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=self.path.parent,
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(
                    document,
                    handle,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
        except BaseException:
            try:
                temp_path.unlink(missing_ok=True)
            finally:
                raise
