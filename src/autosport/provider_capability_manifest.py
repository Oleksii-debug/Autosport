"""Fail-closed provider capability manifest projection.

The canonical :class:'BookmakerCapabilityProfile' remains the authority for every
capability it already owns.  This module projects that authority into a complete
manifest and adds narrowly-scoped evidence-backed facets that the canonical profile
does not currently model.  Unknown or absent evidence is always explicit
''NOT_PROVEN''; it never defaults to support.

This module is descriptive only.  It performs no provider I/O, handles no credentials,
and grants no provider-write, execution, settlement, legal, real-money, or release
authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from hashlib import sha256
import json

from .bookmaker_capability import (
    BookmakerCapability,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
)
from .bookmaker_integration_boundary import (
    BookmakerIntegrationEvidence,
    BookmakerIntegrationKind,
)


class ProviderCapabilityManifestError(ValueError):
    """Raised when provider capability manifest evidence is malformed or widened."""


class ProviderManifestCapability(str, Enum):
    """Complete manifest vocabulary.

    Capabilities already represented by 'BookmakerCapability' are projected from
    that canonical profile.  The remaining entries require their own explicit
    evidence before they may become conclusive.
    """

    SPORTS = "sports"
    MARKETS = "markets"
    PREMATCH_QUOTES = "prematch_quotes"
    LIVE_QUOTES = "live_quotes"
    POLL = "poll"
    STREAM = "stream"
    PROVIDER_TIMESTAMPS = "provider_timestamps"
    LIMITS = "limits"
    ACCOUNT_IDENTITY = "account_identity"
    BALANCE = "balance"
    BETSLIP = "betslip"
    OPEN_POSITIONS = "open_positions"
    SETTLED_POSITIONS = "settled_positions"
    SUBMIT = "submit"
    IMMEDIATE_ACK = "immediate_ack"
    READBACK = "readback"
    IDEMPOTENCY = "idempotency"
    SETTLEMENT = "settlement"
    CASHOUT = "cashout"
    CANCEL_BET = "cancel_bet"


class ProviderManifestState(str, Enum):
    """Manifest truth state.

    'NOT_PROVEN' is intentionally distinct from 'UNSUPPORTED'.
    """

    PROVEN = "proven"
    UNSUPPORTED = "unsupported"
    NOT_PROVEN = "not_proven"


class ProviderManifestFactAuthority(str, Enum):
    """Authority that owns a manifest fact."""

    CANONICAL_PROFILE = "canonical_profile"
    EXPLICIT_EVIDENCE = "explicit_evidence"
    NOT_PROVEN = "not_proven"


_CANONICAL_CAPABILITY_MAP: dict[ProviderManifestCapability, BookmakerCapability] = {
    ProviderManifestCapability.PREMATCH_QUOTES: BookmakerCapability.PREMATCH_QUOTES_READ,
    ProviderManifestCapability.LIVE_QUOTES: BookmakerCapability.LIVE_QUOTES_READ,
    ProviderManifestCapability.LIMITS: BookmakerCapability.LIMITS_READ,
    ProviderManifestCapability.ACCOUNT_IDENTITY: BookmakerCapability.ACCOUNT_IDENTITY_READ,
    ProviderManifestCapability.BALANCE: BookmakerCapability.BALANCE_READ,
    ProviderManifestCapability.BETSLIP: BookmakerCapability.BETSLIP_READ,
    ProviderManifestCapability.OPEN_POSITIONS: BookmakerCapability.OPEN_POSITIONS_READ,
    ProviderManifestCapability.SETTLED_POSITIONS: BookmakerCapability.SETTLED_POSITIONS_READ,
    ProviderManifestCapability.SUBMIT: BookmakerCapability.PLACE_BET,
    ProviderManifestCapability.READBACK: BookmakerCapability.BET_READBACK,
    ProviderManifestCapability.CASHOUT: BookmakerCapability.CASHOUT,
    ProviderManifestCapability.CANCEL_BET: BookmakerCapability.CANCEL_BET,
}

_EXTENSION_CAPABILITIES = frozenset(ProviderManifestCapability) - frozenset(
    _CANONICAL_CAPABILITY_MAP
)


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ProviderCapabilityManifestError(
            f"{field} must be a non-empty trimmed string"
        )
    return value


def _sha256(value: object, field: str) -> str:
    text = _text(value, field)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ProviderCapabilityManifestError(
            f"{field} must be a lowercase 64-character SHA-256 hex digest"
        )
    return text


def _timestamp(value: object, field: str) -> datetime:
    text = _text(value, field)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ProviderCapabilityManifestError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProviderCapabilityManifestError(
            f"{field} must include a timezone offset"
        )
    return parsed


def _positive_int(value: object, field: str) -> int:
    if type(value) is not int or value < 1:
        raise ProviderCapabilityManifestError(f"{field} must be a positive integer")
    return value


def _canonical_json(payload: dict[str, object]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _validate_exact_profile(profile: BookmakerCapabilityProfile) -> None:
    if type(profile) is not BookmakerCapabilityProfile:
        raise ProviderCapabilityManifestError(
            "profile must be an exact BookmakerCapabilityProfile"
        )
    if type(profile.facts) is not tuple:
        raise ProviderCapabilityManifestError("profile.facts must be an exact tuple")
    if any(type(fact) is not BookmakerCapabilityFact for fact in profile.facts):
        raise ProviderCapabilityManifestError(
            "profile facts must be exact BookmakerCapabilityFact values"
        )


def _profile_state(
    profile: BookmakerCapabilityProfile,
    capability: BookmakerCapability,
) -> ProviderManifestState:
    state = profile.state_of(capability)
    if state is BookmakerCapabilityState.SUPPORTED:
        return ProviderManifestState.PROVEN
    if state is BookmakerCapabilityState.UNSUPPORTED:
        return ProviderManifestState.UNSUPPORTED
    if state is BookmakerCapabilityState.UNKNOWN:
        return ProviderManifestState.NOT_PROVEN
    raise ProviderCapabilityManifestError("canonical profile returned an unknown state")


@dataclass(frozen=True, slots=True)
class ProviderCapabilityEvidenceRef:
    """Secret-free immutable reference to one exact capability-evidence payload."""

    kind: str
    evidence_ref: str
    evidence_sha256: str
    observed_at: str
    profile_id: str
    integration_evidence_id: str

    def __post_init__(self) -> None:
        _text(self.kind, "kind")
        _text(self.evidence_ref, "evidence_ref")
        _sha256(self.evidence_sha256, "evidence_sha256")
        _timestamp(self.observed_at, "observed_at")
        _sha256(self.profile_id, "profile_id")
        _sha256(self.integration_evidence_id, "integration_evidence_id")

    @property
    def evidence_id(self) -> str:
        encoded = _canonical_json(self.to_canonical_dict()).encode("utf-8")
        return sha256(encoded).hexdigest()

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            "evidence_ref": self.evidence_ref,
            "evidence_sha256": self.evidence_sha256,
            "integration_evidence_id": self.integration_evidence_id,
            "kind": self.kind,
            "observed_at": self.observed_at,
            "profile_id": self.profile_id,
        }


@dataclass(frozen=True, slots=True)
class ProviderCapabilityManifestFact:
    """One explicit manifest fact.

    Canonical-profile facts cannot carry caller evidence.  Extension facts become
    conclusive only when bound to an exact evidence reference.
    """

    capability: ProviderManifestCapability
    state: ProviderManifestState
    authority: ProviderManifestFactAuthority
    values: tuple[str, ...] = ()
    evidence: ProviderCapabilityEvidenceRef | None = None

    def __post_init__(self) -> None:
        if type(self.capability) is not ProviderManifestCapability:
            raise ProviderCapabilityManifestError(
                "capability must be an exact ProviderManifestCapability value"
            )
        if type(self.state) is not ProviderManifestState:
            raise ProviderCapabilityManifestError(
                "state must be an exact ProviderManifestState value"
            )
        if type(self.authority) is not ProviderManifestFactAuthority:
            raise ProviderCapabilityManifestError(
                "authority must be an exact ProviderManifestFactAuthority value"
            )
        if type(self.values) is not tuple:
            raise ProviderCapabilityManifestError("values must be an exact tuple")
        if any(type(value) is not str for value in self.values):
            raise ProviderCapabilityManifestError(
                "values must contain only exact strings"
            )
        normalized_values = tuple(sorted(_text(value, "values") for value in self.values))
        if len(normalized_values) != len(set(normalized_values)):
            raise ProviderCapabilityManifestError("values cannot contain duplicates")
        if self.values != normalized_values:
            raise ProviderCapabilityManifestError(
                "values must be unique and in canonical sorted order"
            )

        if self.values and self.capability not in {
            ProviderManifestCapability.SPORTS,
            ProviderManifestCapability.MARKETS,
        }:
            raise ProviderCapabilityManifestError(
                "values are only valid for sports or markets capability facts"
            )
        if self.state is not ProviderManifestState.PROVEN and self.values:
            raise ProviderCapabilityManifestError(
                "only a proven capability may carry concrete values"
            )
        if (
            self.capability
            in {ProviderManifestCapability.SPORTS, ProviderManifestCapability.MARKETS}
            and self.state is ProviderManifestState.PROVEN
            and not self.values
        ):
            raise ProviderCapabilityManifestError(
                "proven sports/markets capability requires concrete values"
            )

        if self.authority is ProviderManifestFactAuthority.EXPLICIT_EVIDENCE:
            if self.capability not in _EXTENSION_CAPABILITIES:
                raise ProviderCapabilityManifestError(
                    "canonical-profile capability cannot be caller-overridden"
                )
            if type(self.evidence) is not ProviderCapabilityEvidenceRef:
                raise ProviderCapabilityManifestError(
                    "explicit evidence authority requires an exact evidence reference"
                )
            if self.state is ProviderManifestState.NOT_PROVEN:
                raise ProviderCapabilityManifestError(
                    "not_proven cannot claim explicit positive/negative evidence"
                )
        elif self.evidence is not None:
            raise ProviderCapabilityManifestError(
                "only explicit-evidence facts may carry evidence"
            )

        if self.authority is ProviderManifestFactAuthority.CANONICAL_PROFILE:
            if self.capability not in _CANONICAL_CAPABILITY_MAP:
                raise ProviderCapabilityManifestError(
                    "canonical-profile authority is invalid for this extension capability"
                )
        elif self.authority is ProviderManifestFactAuthority.NOT_PROVEN:
            if self.state is not ProviderManifestState.NOT_PROVEN:
                raise ProviderCapabilityManifestError(
                    "not_proven authority requires NOT_PROVEN state"
                )

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            "authority": self.authority.value,
            "capability": self.capability.value,
            "evidence": (
                self.evidence.to_canonical_dict() if self.evidence is not None else None
            ),
            "state": self.state.value,
            "values": list(self.values),
        }


@dataclass(frozen=True, slots=True)
class ProviderCapabilityManifest:
    """Versioned complete capability projection for one exact provider profile."""

    manifest_ref: str
    manifest_version: int
    profile: BookmakerCapabilityProfile
    integration: BookmakerIntegrationEvidence
    facts: tuple[ProviderCapabilityManifestFact, ...]
    observed_at: str
    source_ref: str
    source_payload_sha256: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        _text(self.manifest_ref, "manifest_ref")
        _positive_int(self.manifest_version, "manifest_version")
        _validate_exact_profile(self.profile)
        if type(self.integration) is not BookmakerIntegrationEvidence:
            raise ProviderCapabilityManifestError(
                "integration must be an exact BookmakerIntegrationEvidence"
            )
        self.integration.verify_profile(self.profile)

        profile_at = _timestamp(self.profile.observed_at, "profile.observed_at")
        integration_at = _timestamp(
            self.integration.observed_at,
            "integration.observed_at",
        )
        observed_at = _timestamp(self.observed_at, "observed_at")
        if observed_at < profile_at or observed_at < integration_at:
            raise ProviderCapabilityManifestError(
                "manifest observed_at cannot predate bound profile/integration evidence"
            )
        _text(self.source_ref, "source_ref")
        _sha256(self.source_payload_sha256, "source_payload_sha256")
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ProviderCapabilityManifestError("schema_version must be exactly 1")

        self._validate_facts(integration_at, observed_at)
        self._validate_dependencies()

    def _validate_facts(
        self,
        integration_at: datetime,
        observed_at: datetime,
    ) -> None:
        if type(self.facts) is not tuple:
            raise ProviderCapabilityManifestError("facts must be an exact tuple")
        if any(type(fact) is not ProviderCapabilityManifestFact for fact in self.facts):
            raise ProviderCapabilityManifestError(
                "facts must contain exact ProviderCapabilityManifestFact values"
            )
        expected_capabilities = tuple(ProviderManifestCapability)
        actual_capabilities = tuple(fact.capability for fact in self.facts)
        if actual_capabilities != expected_capabilities:
            raise ProviderCapabilityManifestError(
                "facts must contain every manifest capability exactly once in canonical order"
            )

        for fact in self.facts:
            canonical = _CANONICAL_CAPABILITY_MAP.get(fact.capability)
            if canonical is not None:
                if fact.authority is not ProviderManifestFactAuthority.CANONICAL_PROFILE:
                    raise ProviderCapabilityManifestError(
                        f"{fact.capability.value} must remain owned by canonical profile"
                    )
                expected_state = _profile_state(self.profile, canonical)
                if fact.state is not expected_state:
                    raise ProviderCapabilityManifestError(
                        f"{fact.capability.value} contradicts canonical profile state"
                    )
                if fact.values or fact.evidence is not None:
                    raise ProviderCapabilityManifestError(
                        f"{fact.capability.value} canonical projection cannot carry override data"
                    )
            else:
                if fact.state is ProviderManifestState.NOT_PROVEN:
                    if fact.authority is not ProviderManifestFactAuthority.NOT_PROVEN:
                        raise ProviderCapabilityManifestError(
                            f"{fact.capability.value} NOT_PROVEN must be explicit"
                        )
                elif fact.authority is not ProviderManifestFactAuthority.EXPLICIT_EVIDENCE:
                    raise ProviderCapabilityManifestError(
                        f"{fact.capability.value} conclusive state requires explicit evidence"
                    )

            if fact.evidence is not None:
                evidence_at = _timestamp(
                    fact.evidence.observed_at,
                    "fact.evidence.observed_at",
                )
                if evidence_at < integration_at:
                    raise ProviderCapabilityManifestError(
                        f"{fact.capability.value} evidence cannot predate integration evidence"
                    )
                if evidence_at > observed_at:
                    raise ProviderCapabilityManifestError(
                        f"{fact.capability.value} evidence cannot postdate manifest"
                    )
                if fact.evidence.profile_id != self.profile.profile_id:
                    raise ProviderCapabilityManifestError(
                        f"{fact.capability.value} evidence does not bind exact profile"
                    )
                if (
                    fact.evidence.integration_evidence_id
                    != self.integration.evidence_id
                ):
                    raise ProviderCapabilityManifestError(
                        f"{fact.capability.value} evidence does not bind exact integration"
                    )

    def _validate_dependencies(self) -> None:
        quote_read = (
            self.supports(ProviderManifestCapability.PREMATCH_QUOTES)
            or self.supports(ProviderManifestCapability.LIVE_QUOTES)
        )
        for transport in (
            ProviderManifestCapability.POLL,
            ProviderManifestCapability.STREAM,
        ):
            if self.supports(transport) and not quote_read:
                raise ProviderCapabilityManifestError(
                    f"{transport.value} requires proven quote-read capability"
                )

        if (
            self.supports(ProviderManifestCapability.IMMEDIATE_ACK)
            or self.supports(ProviderManifestCapability.IDEMPOTENCY)
        ) and not self.supports(ProviderManifestCapability.SUBMIT):
            raise ProviderCapabilityManifestError(
                "immediate_ack/idempotency requires proven submit capability"
            )

        if (
            self.supports(ProviderManifestCapability.SETTLEMENT)
            and not self.supports(ProviderManifestCapability.SETTLED_POSITIONS)
        ):
            raise ProviderCapabilityManifestError(
                "settlement requires proven settled-position read capability"
            )

    @property
    def integration_kind(self) -> BookmakerIntegrationKind:
        return self.integration.integration_kind

    @property
    def manifest_sha256(self) -> str:
        return sha256(_canonical_json(self.to_canonical_dict()).encode("utf-8")).hexdigest()

    @property
    def manifest_id(self) -> str:
        return self.manifest_sha256

    def state_of(self, capability: ProviderManifestCapability) -> ProviderManifestState:
        if type(capability) is not ProviderManifestCapability:
            raise ProviderCapabilityManifestError(
                "capability must be an exact ProviderManifestCapability value"
            )
        for fact in self.facts:
            if fact.capability is capability:
                return fact.state
        raise ProviderCapabilityManifestError("manifest capability is missing")

    def supports(self, capability: ProviderManifestCapability) -> bool:
        return self.state_of(capability) is ProviderManifestState.PROVEN

    @property
    def provider_write_authorized(self) -> bool:
        return False

    @property
    def execution_authorized(self) -> bool:
        return False

    @property
    def real_money_execution(self) -> bool:
        return False

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            "facts": [fact.to_canonical_dict() for fact in self.facts],
            "integration_evidence_id": self.integration.evidence_id,
            "integration_kind": self.integration.integration_kind.value,
            "manifest_ref": self.manifest_ref,
            "manifest_version": self.manifest_version,
            "observed_at": self.observed_at,
            "profile_id": self.profile.profile_id,
            "profile_version": self.profile.profile_version,
            "schema_version": self.schema_version,
            "source_payload_sha256": self.source_payload_sha256,
            "source_ref": self.source_ref,
        }


def build_provider_capability_manifest(
    profile: BookmakerCapabilityProfile,
    integration: BookmakerIntegrationEvidence,
    *,
    manifest_ref: str,
    manifest_version: int,
    observed_at: str,
    source_ref: str,
    source_payload_sha256: str,
    extension_facts: tuple[ProviderCapabilityManifestFact, ...] = (),
) -> ProviderCapabilityManifest:
    """Build a complete fail-closed manifest over canonical provider truth.

    Missing extension facts are materialized as ''NOT_PROVEN''.  Canonical capabilities
    cannot be supplied in ''extension_facts'' and therefore cannot be caller-overridden.
    """

    _validate_exact_profile(profile)
    if type(integration) is not BookmakerIntegrationEvidence:
        raise ProviderCapabilityManifestError(
            "integration must be an exact BookmakerIntegrationEvidence"
        )
    integration.verify_profile(profile)
    if type(extension_facts) is not tuple:
        raise ProviderCapabilityManifestError("extension_facts must be an exact tuple")
    if any(type(fact) is not ProviderCapabilityManifestFact for fact in extension_facts):
        raise ProviderCapabilityManifestError(
            "extension_facts must contain exact ProviderCapabilityManifestFact values"
        )

    by_capability: dict[ProviderManifestCapability, ProviderCapabilityManifestFact] = {}
    for fact in extension_facts:
        if fact.capability in _CANONICAL_CAPABILITY_MAP:
            raise ProviderCapabilityManifestError(
                f"{fact.capability.value} is canonical and cannot be caller-overridden"
            )
        if fact.capability in by_capability:
            raise ProviderCapabilityManifestError(
                f"duplicate extension fact: {fact.capability.value}"
            )
        by_capability[fact.capability] = fact

    facts: list[ProviderCapabilityManifestFact] = []
    for capability in ProviderManifestCapability:
        canonical = _CANONICAL_CAPABILITY_MAP.get(capability)
        if canonical is not None:
            facts.append(
                ProviderCapabilityManifestFact(
                    capability=capability,
                    state=_profile_state(profile, canonical),
                    authority=ProviderManifestFactAuthority.CANONICAL_PROFILE,
                )
            )
            continue

        supplied = by_capability.get(capability)
        if supplied is not None:
            facts.append(supplied)
        else:
            facts.append(
                ProviderCapabilityManifestFact(
                    capability=capability,
                    state=ProviderManifestState.NOT_PROVEN,
                    authority=ProviderManifestFactAuthority.NOT_PROVEN,
                )
            )

    return ProviderCapabilityManifest(
        manifest_ref=manifest_ref,
        manifest_version=manifest_version,
        profile=profile,
        integration=integration,
        facts=tuple(facts),
        observed_at=observed_at,
        source_ref=source_ref,
        source_payload_sha256=source_payload_sha256,
    )
