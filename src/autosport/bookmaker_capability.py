from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from .integrity import atomic_write_json
from .workspace_lock import WorkspaceEconomicLock


REGISTRY_SCHEMA_VERSION = 1
PROFILE_SCHEMA_VERSION = 1
GOVERNANCE_SCHEMA_VERSION = 1


class BookmakerCapabilityError(ValueError):
    """Base error for malformed or unsafe bookmaker capability evidence."""


class BookmakerCapabilityConflictError(BookmakerCapabilityError):
    """Raised when one immutable registry identity is rebound to different evidence."""


class BookmakerCapabilityUnavailableError(BookmakerCapabilityError):
    """Raised when a required read capability is not proven supported."""


class BookmakerCapabilityRegistryCorruptionError(BookmakerCapabilityError):
    """Raised when durable registry bytes cannot be interpreted canonically."""


class CapabilityState(str, Enum):
    UNKNOWN = "unknown"
    UNSUPPORTED = "unsupported"
    SUPPORTED = "supported"


class IntegrationMode(str, Enum):
    UNKNOWN = "unknown"
    OFFICIAL_API = "official_api"
    SANCTIONED_INTERFACE = "sanctioned_interface"
    BROWSER_ADAPTER = "browser_adapter"
    MANUAL_READBACK = "manual_readback"


class CapabilityName(str, Enum):
    PRE_MATCH_QUOTES = "pre_match_quotes"
    LIVE_QUOTES = "live_quotes"
    ACCOUNT_IDENTITY = "account_identity"
    BALANCE = "balance"
    LIMITS = "limits"
    OPEN_POSITIONS = "open_positions"
    SETTLED_POSITIONS = "settled_positions"
    EXTERNAL_RECEIPTS = "external_receipts"
    BET_SLIP_PREPARE = "bet_slip_prepare"
    PLACE_BET = "place_bet"
    CASHOUT = "cashout"
    CANCEL = "cancel"


READ_CAPABILITIES = frozenset(
    {
        CapabilityName.PRE_MATCH_QUOTES,
        CapabilityName.LIVE_QUOTES,
        CapabilityName.ACCOUNT_IDENTITY,
        CapabilityName.BALANCE,
        CapabilityName.LIMITS,
        CapabilityName.OPEN_POSITIONS,
        CapabilityName.SETTLED_POSITIONS,
        CapabilityName.EXTERNAL_RECEIPTS,
    }
)


class AutomationPermission(str, Enum):
    UNKNOWN = "unknown"
    PROHIBITED = "prohibited"
    READ_ONLY_PERMITTED = "read_only_permitted"
    PREPARATION_PERMITTED = "preparation_permitted"
    PLACEMENT_PERMITTED = "placement_permitted"


def _require_text(value: object, name: str) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise BookmakerCapabilityError(f"{name} must be a non-empty trimmed string")
    return value


def _require_positive_int(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise BookmakerCapabilityError(f"{name} must be a positive integer")
    return value


def _require_schema_version(value: object, expected: int, name: str) -> int:
    if type(value) is not int or value != expected:
        raise BookmakerCapabilityError(f"{name} must equal {expected}")
    return value


def _require_utc_timestamp(value: object, name: str) -> str:
    text = _require_text(value, name)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise BookmakerCapabilityError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise BookmakerCapabilityError(f"{name} must be timezone-aware UTC")
    canonical = parsed.astimezone(timezone.utc).isoformat()
    if text != canonical:
        raise BookmakerCapabilityError(
            f"{name} must use canonical UTC ISO-8601 form {canonical!r}"
        )
    return text


def _canonical_tokens(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)):
        raise BookmakerCapabilityError(f"{name} must be a tuple/list of strings")
    items = tuple(_require_text(item, f"{name} item") for item in value)
    if len(set(items)) != len(items):
        raise BookmakerCapabilityError(f"{name} must not contain duplicates")
    return tuple(sorted(items))


def _enum_value(enum_type: type[Enum], value: object, name: str) -> Enum:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise BookmakerCapabilityError(f"{name} is invalid") from exc


def _canonical_sha256(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _expect_keys(payload: object, expected: set[str], name: str) -> dict[str, Any]:
    if type(payload) is not dict:
        raise BookmakerCapabilityError(f"{name} must be a JSON object")
    actual = set(payload)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise BookmakerCapabilityError(
            f"{name} keys mismatch; missing={missing}, extra={extra}"
        )
    return payload


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise BookmakerCapabilityRegistryCorruptionError(
                f"duplicate JSON object key: {key}"
            )
        result[key] = value
    return result


def _reject_nonfinite_json_constant(value: str) -> None:
    raise BookmakerCapabilityRegistryCorruptionError(
        f"non-finite JSON constant: {value}"
    )


@dataclass(frozen=True, slots=True)
class TechnicalCapabilities:
    pre_match_quotes: CapabilityState = CapabilityState.UNKNOWN
    live_quotes: CapabilityState = CapabilityState.UNKNOWN
    account_identity: CapabilityState = CapabilityState.UNKNOWN
    balance: CapabilityState = CapabilityState.UNKNOWN
    limits: CapabilityState = CapabilityState.UNKNOWN
    open_positions: CapabilityState = CapabilityState.UNKNOWN
    settled_positions: CapabilityState = CapabilityState.UNKNOWN
    external_receipts: CapabilityState = CapabilityState.UNKNOWN
    bet_slip_prepare: CapabilityState = CapabilityState.UNKNOWN
    place_bet: CapabilityState = CapabilityState.UNKNOWN
    cashout: CapabilityState = CapabilityState.UNKNOWN
    cancel: CapabilityState = CapabilityState.UNKNOWN

    def __post_init__(self) -> None:
        for field in fields(self):
            if not isinstance(getattr(self, field.name), CapabilityState):
                raise BookmakerCapabilityError(
                    f"technical capability {field.name} must be CapabilityState"
                )

    def to_payload(self) -> dict[str, str]:
        return {field.name: getattr(self, field.name).value for field in fields(self)}

    @classmethod
    def from_payload(cls, payload: object) -> "TechnicalCapabilities":
        expected = {field.name for field in fields(cls)}
        data = _expect_keys(payload, expected, "technical_capabilities")
        return cls(
            **{
                field.name: _enum_value(
                    CapabilityState,
                    data[field.name],
                    f"technical_capabilities.{field.name}",
                )
                for field in fields(cls)
            }
        )

    def state(self, capability: CapabilityName) -> CapabilityState:
        if not isinstance(capability, CapabilityName):
            raise BookmakerCapabilityError("capability must be CapabilityName")
        return getattr(self, capability.value)


@dataclass(frozen=True, slots=True)
class BookmakerCapabilityProfile:
    provider_id: str
    adapter_id: str
    account_scope: str
    profile_version: int
    observed_at_utc: str
    source_ref: str
    integration_mode: IntegrationMode = IntegrationMode.UNKNOWN
    supported_sports: tuple[str, ...] = ()
    supported_markets: tuple[str, ...] = ()
    technical: TechnicalCapabilities = TechnicalCapabilities()
    schema_version: int = PROFILE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema_version(
            self.schema_version, PROFILE_SCHEMA_VERSION, "profile schema_version"
        )
        _require_text(self.provider_id, "provider_id")
        _require_text(self.adapter_id, "adapter_id")
        _require_text(self.account_scope, "account_scope")
        _require_positive_int(self.profile_version, "profile_version")
        _require_utc_timestamp(self.observed_at_utc, "observed_at_utc")
        _require_text(self.source_ref, "source_ref")
        if not isinstance(self.integration_mode, IntegrationMode):
            raise BookmakerCapabilityError("integration_mode must be IntegrationMode")
        if not isinstance(self.technical, TechnicalCapabilities):
            raise BookmakerCapabilityError("technical must be TechnicalCapabilities")
        object.__setattr__(
            self,
            "supported_sports",
            _canonical_tokens(self.supported_sports, "supported_sports"),
        )
        object.__setattr__(
            self,
            "supported_markets",
            _canonical_tokens(self.supported_markets, "supported_markets"),
        )

    def identity_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "provider_id": self.provider_id,
            "adapter_id": self.adapter_id,
            "account_scope": self.account_scope,
            "profile_version": self.profile_version,
            "observed_at_utc": self.observed_at_utc,
            "source_ref": self.source_ref,
            "integration_mode": self.integration_mode.value,
            "supported_sports": list(self.supported_sports),
            "supported_markets": list(self.supported_markets),
            "technical_capabilities": self.technical.to_payload(),
        }

    @property
    def profile_id(self) -> str:
        return _canonical_sha256(self.identity_payload())

    def to_payload(self) -> dict[str, Any]:
        return {"profile_id": self.profile_id, **self.identity_payload()}

    @classmethod
    def from_payload(cls, payload: object) -> "BookmakerCapabilityProfile":
        expected = {
            "profile_id",
            "schema_version",
            "provider_id",
            "adapter_id",
            "account_scope",
            "profile_version",
            "observed_at_utc",
            "source_ref",
            "integration_mode",
            "supported_sports",
            "supported_markets",
            "technical_capabilities",
        }
        data = _expect_keys(payload, expected, "bookmaker capability profile")
        profile = cls(
            provider_id=data["provider_id"],
            adapter_id=data["adapter_id"],
            account_scope=data["account_scope"],
            profile_version=data["profile_version"],
            observed_at_utc=data["observed_at_utc"],
            source_ref=data["source_ref"],
            integration_mode=_enum_value(
                IntegrationMode, data["integration_mode"], "integration_mode"
            ),
            supported_sports=data["supported_sports"],
            supported_markets=data["supported_markets"],
            technical=TechnicalCapabilities.from_payload(data["technical_capabilities"]),
            schema_version=data["schema_version"],
        )
        supplied_id = _require_text(data["profile_id"], "profile_id")
        if supplied_id != profile.profile_id:
            raise BookmakerCapabilityError(
                "profile_id does not match immutable capability evidence"
            )
        return profile


@dataclass(frozen=True, slots=True)
class BookmakerGovernanceEvidence:
    provider_id: str
    account_scope: str
    governance_version: int
    jurisdiction: str
    terms_ref: str
    evidence_ref: str
    observed_at_utc: str
    automation_permission: AutomationPermission
    schema_version: int = GOVERNANCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema_version(
            self.schema_version,
            GOVERNANCE_SCHEMA_VERSION,
            "governance schema_version",
        )
        _require_text(self.provider_id, "governance provider_id")
        _require_text(self.account_scope, "governance account_scope")
        _require_positive_int(self.governance_version, "governance_version")
        _require_text(self.jurisdiction, "jurisdiction")
        _require_text(self.terms_ref, "terms_ref")
        _require_text(self.evidence_ref, "evidence_ref")
        _require_utc_timestamp(self.observed_at_utc, "governance observed_at_utc")
        if not isinstance(self.automation_permission, AutomationPermission):
            raise BookmakerCapabilityError(
                "automation_permission must be AutomationPermission"
            )

    def identity_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "provider_id": self.provider_id,
            "account_scope": self.account_scope,
            "governance_version": self.governance_version,
            "jurisdiction": self.jurisdiction,
            "terms_ref": self.terms_ref,
            "evidence_ref": self.evidence_ref,
            "observed_at_utc": self.observed_at_utc,
            "automation_permission": self.automation_permission.value,
        }

    @property
    def governance_id(self) -> str:
        return _canonical_sha256(self.identity_payload())

    def to_payload(self) -> dict[str, Any]:
        return {"governance_id": self.governance_id, **self.identity_payload()}

    @classmethod
    def from_payload(cls, payload: object) -> "BookmakerGovernanceEvidence":
        expected = {
            "governance_id",
            "schema_version",
            "provider_id",
            "account_scope",
            "governance_version",
            "jurisdiction",
            "terms_ref",
            "evidence_ref",
            "observed_at_utc",
            "automation_permission",
        }
        data = _expect_keys(payload, expected, "bookmaker governance evidence")
        evidence = cls(
            provider_id=data["provider_id"],
            account_scope=data["account_scope"],
            governance_version=data["governance_version"],
            jurisdiction=data["jurisdiction"],
            terms_ref=data["terms_ref"],
            evidence_ref=data["evidence_ref"],
            observed_at_utc=data["observed_at_utc"],
            automation_permission=_enum_value(
                AutomationPermission,
                data["automation_permission"],
                "automation_permission",
            ),
            schema_version=data["schema_version"],
        )
        supplied_id = _require_text(data["governance_id"], "governance_id")
        if supplied_id != evidence.governance_id:
            raise BookmakerCapabilityError(
                "governance_id does not match immutable governance evidence"
            )
        return evidence


class BookmakerCapabilityRegistry:
    """Durable append-only capability/governance evidence registry.

    Technical capability and governance evidence are deliberately separate. This
    registry can prove a read capability exists; it never grants execution authority.
    All durable writes reuse the repository's existing cross-process workspace lock
    and atomic JSON publication primitive.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._profiles: tuple[BookmakerCapabilityProfile, ...] = ()
        self._governance: tuple[BookmakerGovernanceEvidence, ...] = ()
        self.reload()

    @property
    def profiles(self) -> tuple[BookmakerCapabilityProfile, ...]:
        return self._profiles

    @property
    def governance(self) -> tuple[BookmakerGovernanceEvidence, ...]:
        return self._governance

    def reload(self) -> None:
        profiles, governance = self._read_or_empty()
        self._profiles = profiles
        self._governance = governance

    def register_profile(self, profile: BookmakerCapabilityProfile) -> bool:
        if not isinstance(profile, BookmakerCapabilityProfile):
            raise BookmakerCapabilityError("profile must be BookmakerCapabilityProfile")
        with WorkspaceEconomicLock(self.path.parent):
            profiles, governance = self._read_or_empty()
            key = (profile.provider_id, profile.account_scope, profile.profile_version)
            for existing in profiles:
                existing_key = (
                    existing.provider_id,
                    existing.account_scope,
                    existing.profile_version,
                )
                if existing_key != key:
                    continue
                if existing.profile_id == profile.profile_id:
                    self._profiles = profiles
                    self._governance = governance
                    return False
                raise BookmakerCapabilityConflictError(
                    "capability profile version is already bound to different evidence"
                )
            updated_profiles = tuple(
                sorted(
                    (*profiles, profile),
                    key=lambda item: (
                        item.provider_id,
                        item.account_scope,
                        item.profile_version,
                        item.profile_id,
                    ),
                )
            )
            self._publish(updated_profiles, governance)
            self._profiles = updated_profiles
            self._governance = governance
            return True

    def register_governance(self, evidence: BookmakerGovernanceEvidence) -> bool:
        if not isinstance(evidence, BookmakerGovernanceEvidence):
            raise BookmakerCapabilityError(
                "evidence must be BookmakerGovernanceEvidence"
            )
        with WorkspaceEconomicLock(self.path.parent):
            profiles, governance = self._read_or_empty()
            key = (
                evidence.provider_id,
                evidence.account_scope,
                evidence.governance_version,
            )
            for existing in governance:
                existing_key = (
                    existing.provider_id,
                    existing.account_scope,
                    existing.governance_version,
                )
                if existing_key != key:
                    continue
                if existing.governance_id == evidence.governance_id:
                    self._profiles = profiles
                    self._governance = governance
                    return False
                raise BookmakerCapabilityConflictError(
                    "governance version is already bound to different evidence"
                )
            updated_governance = tuple(
                sorted(
                    (*governance, evidence),
                    key=lambda item: (
                        item.provider_id,
                        item.account_scope,
                        item.governance_version,
                        item.governance_id,
                    ),
                )
            )
            self._publish(profiles, updated_governance)
            self._profiles = profiles
            self._governance = updated_governance
            return True

    def latest_profile(
        self,
        provider_id: str,
        account_scope: str,
    ) -> BookmakerCapabilityProfile | None:
        provider = _require_text(provider_id, "provider_id")
        account = _require_text(account_scope, "account_scope")
        self.reload()
        candidates = tuple(
            item
            for item in self._profiles
            if item.provider_id == provider and item.account_scope == account
        )
        if not candidates:
            return None
        return max(candidates, key=lambda item: item.profile_version)

    def latest_governance(
        self,
        provider_id: str,
        account_scope: str,
    ) -> BookmakerGovernanceEvidence | None:
        provider = _require_text(provider_id, "provider_id")
        account = _require_text(account_scope, "account_scope")
        self.reload()
        candidates = tuple(
            item
            for item in self._governance
            if item.provider_id == provider and item.account_scope == account
        )
        if not candidates:
            return None
        return max(candidates, key=lambda item: item.governance_version)

    def require_read_supported(
        self,
        provider_id: str,
        account_scope: str,
        capability: CapabilityName,
    ) -> BookmakerCapabilityProfile:
        if not isinstance(capability, CapabilityName):
            raise BookmakerCapabilityError("capability must be CapabilityName")
        if capability not in READ_CAPABILITIES:
            raise BookmakerCapabilityError(
                f"{capability.value} is not a read-only capability"
            )
        profile = self.latest_profile(provider_id, account_scope)
        if profile is None:
            raise BookmakerCapabilityUnavailableError(
                "no bookmaker capability profile is available"
            )
        state = profile.technical.state(capability)
        if state is not CapabilityState.SUPPORTED:
            raise BookmakerCapabilityUnavailableError(
                f"{capability.value} is {state.value}, not proven supported"
            )
        return profile

    def _read_or_empty(
        self,
    ) -> tuple[
        tuple[BookmakerCapabilityProfile, ...],
        tuple[BookmakerGovernanceEvidence, ...],
    ]:
        if not self.path.exists():
            return (), ()
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                payload = json.load(
                    handle,
                    object_pairs_hook=_reject_duplicate_json_keys,
                    parse_constant=_reject_nonfinite_json_constant,
                )
            data = _expect_keys(
                payload,
                {"schema_version", "profiles", "governance"},
                "bookmaker capability registry",
            )
            _require_schema_version(
                data["schema_version"],
                REGISTRY_SCHEMA_VERSION,
                "registry schema_version",
            )
            if type(data["profiles"]) is not list:
                raise BookmakerCapabilityError("registry profiles must be a list")
            if type(data["governance"]) is not list:
                raise BookmakerCapabilityError("registry governance must be a list")
            profiles = tuple(
                BookmakerCapabilityProfile.from_payload(item)
                for item in data["profiles"]
            )
            governance = tuple(
                BookmakerGovernanceEvidence.from_payload(item)
                for item in data["governance"]
            )
            self._validate_loaded_profiles(profiles)
            self._validate_loaded_governance(governance)
            return profiles, governance
        except BookmakerCapabilityRegistryCorruptionError:
            raise
        except (OSError, json.JSONDecodeError, BookmakerCapabilityError) as exc:
            raise BookmakerCapabilityRegistryCorruptionError(
                "bookmaker capability registry is corrupt or non-canonical"
            ) from exc

    @staticmethod
    def _validate_loaded_profiles(
        profiles: Iterable[BookmakerCapabilityProfile],
    ) -> None:
        seen_ids: set[str] = set()
        version_bindings: dict[tuple[str, str, int], str] = {}
        previous_sort_key: tuple[str, str, int, str] | None = None
        for profile in profiles:
            if profile.profile_id in seen_ids:
                raise BookmakerCapabilityRegistryCorruptionError(
                    "duplicate profile_id in registry"
                )
            seen_ids.add(profile.profile_id)
            binding_key = (
                profile.provider_id,
                profile.account_scope,
                profile.profile_version,
            )
            previous_id = version_bindings.get(binding_key)
            if previous_id is not None and previous_id != profile.profile_id:
                raise BookmakerCapabilityRegistryCorruptionError(
                    "profile version has conflicting immutable evidence"
                )
            version_bindings[binding_key] = profile.profile_id
            sort_key = (*binding_key, profile.profile_id)
            if previous_sort_key is not None and sort_key <= previous_sort_key:
                raise BookmakerCapabilityRegistryCorruptionError(
                    "profiles are not in canonical order"
                )
            previous_sort_key = sort_key

    @staticmethod
    def _validate_loaded_governance(
        governance: Iterable[BookmakerGovernanceEvidence],
    ) -> None:
        seen_ids: set[str] = set()
        version_bindings: dict[tuple[str, str, int], str] = {}
        previous_sort_key: tuple[str, str, int, str] | None = None
        for evidence in governance:
            if evidence.governance_id in seen_ids:
                raise BookmakerCapabilityRegistryCorruptionError(
                    "duplicate governance_id in registry"
                )
            seen_ids.add(evidence.governance_id)
            binding_key = (
                evidence.provider_id,
                evidence.account_scope,
                evidence.governance_version,
            )
            previous_id = version_bindings.get(binding_key)
            if previous_id is not None and previous_id != evidence.governance_id:
                raise BookmakerCapabilityRegistryCorruptionError(
                    "governance version has conflicting immutable evidence"
                )
            version_bindings[binding_key] = evidence.governance_id
            sort_key = (*binding_key, evidence.governance_id)
            if previous_sort_key is not None and sort_key <= previous_sort_key:
                raise BookmakerCapabilityRegistryCorruptionError(
                    "governance evidence is not in canonical order"
                )
            previous_sort_key = sort_key

    def _publish(
        self,
        profiles: tuple[BookmakerCapabilityProfile, ...],
        governance: tuple[BookmakerGovernanceEvidence, ...],
    ) -> None:
        atomic_write_json(
            self.path,
            {
                "schema_version": REGISTRY_SCHEMA_VERSION,
                "profiles": [profile.to_payload() for profile in profiles],
                "governance": [evidence.to_payload() for evidence in governance],
            },
        )
