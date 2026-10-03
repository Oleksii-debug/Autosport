"""Fail-closed freshness lifecycle for canonical bookmaker capability profiles."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum, IntEnum
from hashlib import sha256
import json
from typing import Mapping
import weakref

from . import betdaq_account_readonly as _betdaq_account
from .bookmaker_capability import (
    BookmakerCapability,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
)


_BETDAQ_CLIENT_TYPE = _betdaq_account.BetdaqAccountReadOnlyClient
_BETDAQ_ACCOUNT_EVIDENCE_TYPE = _betdaq_account.BetdaqAccountEvidence
_BETDAQ_ACCOUNT_CONTEXT_TYPE = _betdaq_account.BetdaqAuthenticatedAccountContext
_BETDAQ_CREDENTIAL_TYPE = _betdaq_account.BetdaqCredentials
_BETDAQ_TRANSPORT_TYPE = _betdaq_account.UrllibBetdaqSoapTransport
_BETDAQ_CANONICAL_HTTPS_POST = _betdaq_account._CANONICAL_HTTPS_POST
_BETDAQ_CANONICAL_HTTPS_POST_CODE = _BETDAQ_CANONICAL_HTTPS_POST.__code__
_BETDAQ_ERROR_TYPE = _betdaq_account.BetdaqAccountReadOnlyError
_BETDAQ_READ_ACCOUNT_EVIDENCE = _BETDAQ_CLIENT_TYPE.read_account_evidence
_BETDAQ_READ_ACCOUNT_EVIDENCE_CODE = _BETDAQ_READ_ACCOUNT_EVIDENCE.__code__
_BETDAQ_READ_ACCOUNT_BALANCE = _BETDAQ_CLIENT_TYPE.read_account_balance
_BETDAQ_READ_COMPLETE_CURRENT_ORDERS = _BETDAQ_CLIENT_TYPE.read_complete_current_orders
_BETDAQ_READ_BOOTSTRAP_PAGE = _BETDAQ_CLIENT_TYPE.read_bootstrap_page
_BETDAQ_READ_ORDERS_CHANGED_SINCE = _BETDAQ_CLIENT_TYPE.read_orders_changed_since
_BETDAQ_CALL = _BETDAQ_CLIENT_TYPE._call
_BETDAQ_REQUEST_XML = _BETDAQ_CLIENT_TYPE._request_xml
_BETDAQ_OBSERVED_AT = _BETDAQ_CLIENT_TYPE._observed_at
_BETDAQ_CANONICAL_CLIENT_METHODS = (
    ("read_account_evidence", _BETDAQ_READ_ACCOUNT_EVIDENCE, _BETDAQ_READ_ACCOUNT_EVIDENCE.__code__),
    ("read_account_balance", _BETDAQ_READ_ACCOUNT_BALANCE, _BETDAQ_READ_ACCOUNT_BALANCE.__code__),
    (
        "read_complete_current_orders",
        _BETDAQ_READ_COMPLETE_CURRENT_ORDERS,
        _BETDAQ_READ_COMPLETE_CURRENT_ORDERS.__code__,
    ),
    ("read_bootstrap_page", _BETDAQ_READ_BOOTSTRAP_PAGE, _BETDAQ_READ_BOOTSTRAP_PAGE.__code__),
    (
        "read_orders_changed_since",
        _BETDAQ_READ_ORDERS_CHANGED_SINCE,
        _BETDAQ_READ_ORDERS_CHANGED_SINCE.__code__,
    ),
    ("_call", _BETDAQ_CALL, _BETDAQ_CALL.__code__),
    ("_request_xml", _BETDAQ_REQUEST_XML, _BETDAQ_REQUEST_XML.__code__),
    ("_observed_at", _BETDAQ_OBSERVED_AT, _BETDAQ_OBSERVED_AT.__code__),
)
_BETDAQ_REQUIRE_CANONICAL_TRANSPORT = _betdaq_account._require_canonical_account_transport
_BETDAQ_REQUIRE_CANONICAL_TRANSPORT_CODE = _BETDAQ_REQUIRE_CANONICAL_TRANSPORT.__code__
_BETDAQ_AUTHENTICATED_CONTEXT = _betdaq_account._authenticated_account_context
_BETDAQ_AUTHENTICATED_CONTEXT_CODE = _BETDAQ_AUTHENTICATED_CONTEXT.__code__
_BETDAQ_ADAPTER_ID = _betdaq_account.ADAPTER_ID
_BETDAQ_ADAPTER_VERSION = _betdaq_account.ADAPTER_VERSION
_BETDAQ_SECURE_ENDPOINT = _betdaq_account._SECURE_ENDPOINT
_BETDAQ_VENUE_ID = "betdaq"

BETDAQ_AUTHENTICATED_VALIDATION_POLICY_VERSION = "betdaq-authenticated-readonly-v1"
BETDAQ_AUTHENTICATED_SOURCE_CONTRACT_REF = (
    f"betdaq-secure-account-readonly/{_BETDAQ_ADAPTER_VERSION}"
)
BETDAQ_AUTHENTICATED_MAX_OBSERVATION_AGE_SECONDS = 3600


class CapabilityEvidenceError(ValueError):
    """Lifecycle evidence is malformed or conflicts with existing authority."""


class CapabilityEvidenceStrength(IntEnum):
    UNPROVEN = 0
    DOCUMENTED_ONLY = 1
    OBSERVED_PUBLIC = 2
    OBSERVED_AUTHENTICATED = 3
    OBSERVED_ACCOUNT_SCOPED = 4
    EXECUTION_PROVEN = 5


class CapabilityEvidenceSource(str, Enum):
    OFFICIAL_DOCUMENT = "official_document"
    PUBLIC_OBSERVATION = "public_observation"
    AUTHENTICATED_OBSERVATION = "authenticated_observation"
    ACCOUNT_OBSERVATION = "account_observation"


class CapabilityLifecycleState(str, Enum):
    CURRENT = "current"
    REVALIDATION_REQUIRED = "revalidation_required"
    STALE = "stale"
    REVOKED_OR_UNSUPPORTED = "revoked_or_unsupported"
    UNKNOWN = "unknown"


class CapabilityAvailabilityState(str, Enum):
    AVAILABLE = "available"
    DEGRADED = "degraded"
    TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"
    UNKNOWN = "unknown"


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise CapabilityEvidenceError(f"{field} must be a non-empty trimmed string")
    return value


def _optional_text(value: object, field: str) -> str | None:
    return None if value is None else _text(value, field)


def _time(value: object, field: str) -> datetime:
    text = _text(value, field)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise CapabilityEvidenceError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CapabilityEvidenceError(f"{field} must include a timezone offset")
    return parsed


def _sha(value: object, field: str) -> str:
    text = _text(value, field)
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        raise CapabilityEvidenceError(f"{field} must be a lowercase SHA-256 digest")
    return text


def _hash(payload: Mapping[str, object]) -> str:
    raw = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return sha256(raw).hexdigest()


@dataclass(frozen=True, slots=True)
class CapabilityScope:
    venue_id: str
    account_id: str | None
    environment: str
    jurisdiction: str | None = None
    sport: str | None = None
    market_family: str | None = None
    live_mode: str | None = None
    credential_identity: str | None = None

    def __post_init__(self) -> None:
        _text(self.venue_id, "venue_id")
        _optional_text(self.account_id, "account_id")
        _text(self.environment, "environment")
        for field in (
            "jurisdiction",
            "sport",
            "market_family",
            "live_mode",
            "credential_identity",
        ):
            _optional_text(getattr(self, field), field)

    def payload(self) -> dict[str, object]:
        return {
            "venue_id": self.venue_id,
            "account_id": self.account_id,
            "environment": self.environment,
            "jurisdiction": self.jurisdiction,
            "sport": self.sport,
            "market_family": self.market_family,
            "live_mode": self.live_mode,
            "credential_identity": self.credential_identity,
        }


@dataclass(frozen=True, slots=True, weakref_slot=True)
class CapabilityEvidence:
    profile_id: str
    capability: BookmakerCapability
    support_state: BookmakerCapabilityState
    strength: CapabilityEvidenceStrength
    source: CapabilityEvidenceSource
    scope: CapabilityScope
    observed_at: str
    committed_at: str
    review_due_at: str
    validation_policy_version: str
    source_contract_ref: str
    source_payload_sha256: str
    provider_expires_at: str | None = None
    predecessor_id: str | None = None

    def __post_init__(self) -> None:
        _sha(self.profile_id, "profile_id")
        if not isinstance(self.capability, BookmakerCapability):
            raise CapabilityEvidenceError("capability must be BookmakerCapability")
        if not isinstance(self.support_state, BookmakerCapabilityState):
            raise CapabilityEvidenceError("support_state must be BookmakerCapabilityState")
        if not isinstance(self.strength, CapabilityEvidenceStrength):
            raise CapabilityEvidenceError("strength must be CapabilityEvidenceStrength")
        if self.strength is CapabilityEvidenceStrength.EXECUTION_PROVEN:
            raise CapabilityEvidenceError(
                "EXECUTION_PROVEN requires separate real execution authority"
            )
        if not isinstance(self.source, CapabilityEvidenceSource):
            raise CapabilityEvidenceError("source must be CapabilityEvidenceSource")
        if type(self.scope) is not CapabilityScope:
            raise CapabilityEvidenceError("scope must be exact CapabilityScope")
        observed = _time(self.observed_at, "observed_at")
        committed = _time(self.committed_at, "committed_at")
        due = _time(self.review_due_at, "review_due_at")
        if committed < observed:
            raise CapabilityEvidenceError("committed_at cannot precede observed_at")
        if due <= committed:
            raise CapabilityEvidenceError("review_due_at must be after committed_at")
        if self.provider_expires_at is not None:
            if _time(self.provider_expires_at, "provider_expires_at") <= committed:
                raise CapabilityEvidenceError(
                    "provider_expires_at must be after committed_at"
                )
        _text(self.validation_policy_version, "validation_policy_version")
        _text(self.source_contract_ref, "source_contract_ref")
        _sha(self.source_payload_sha256, "source_payload_sha256")
        if self.predecessor_id is not None:
            _sha(self.predecessor_id, "predecessor_id")
        if (
            self.strength >= CapabilityEvidenceStrength.OBSERVED_AUTHENTICATED
            and self.scope.credential_identity is None
        ):
            raise CapabilityEvidenceError(
                "authenticated evidence requires credential_identity"
            )
        if (
            self.strength >= CapabilityEvidenceStrength.OBSERVED_ACCOUNT_SCOPED
            and self.scope.account_id is None
        ):
            raise CapabilityEvidenceError("account-scoped evidence requires account_id")
        expected = {
            CapabilityEvidenceStrength.DOCUMENTED_ONLY:
                CapabilityEvidenceSource.OFFICIAL_DOCUMENT,
            CapabilityEvidenceStrength.OBSERVED_PUBLIC:
                CapabilityEvidenceSource.PUBLIC_OBSERVATION,
            CapabilityEvidenceStrength.OBSERVED_AUTHENTICATED:
                CapabilityEvidenceSource.AUTHENTICATED_OBSERVATION,
            CapabilityEvidenceStrength.OBSERVED_ACCOUNT_SCOPED:
                CapabilityEvidenceSource.ACCOUNT_OBSERVATION,
        }.get(self.strength)
        if expected is not None and self.source is not expected:
            raise CapabilityEvidenceError("strength/source mismatch")

    def payload(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "capability": self.capability.value,
            "support_state": self.support_state.value,
            "strength": self.strength.name,
            "source": self.source.value,
            "scope": self.scope.payload(),
            "observed_at": self.observed_at,
            "committed_at": self.committed_at,
            "review_due_at": self.review_due_at,
            "validation_policy_version": self.validation_policy_version,
            "source_contract_ref": self.source_contract_ref,
            "source_payload_sha256": self.source_payload_sha256,
            "provider_expires_at": self.provider_expires_at,
            "predecessor_id": self.predecessor_id,
        }

    @property
    def evidence_id(self) -> str:
        return _hash(self.payload())


_PRODUCT_ISSUED_POSITIVE: dict[
    int, tuple[weakref.ReferenceType[CapabilityEvidence], str]
] = {}


def _forget_product_issued(
    evidence_id: int, reference: weakref.ReferenceType[CapabilityEvidence]
) -> None:
    current = _PRODUCT_ISSUED_POSITIVE.get(evidence_id)
    if current is not None and current[0] is reference:
        _PRODUCT_ISSUED_POSITIVE.pop(evidence_id, None)


def _remember_product_issued(evidence: CapabilityEvidence) -> CapabilityEvidence:
    identity = id(evidence)
    reference = weakref.ref(
        evidence,
        lambda current, identity=identity: _forget_product_issued(identity, current),
    )
    _PRODUCT_ISSUED_POSITIVE[identity] = (reference, evidence.evidence_id)
    return evidence


def _is_product_issued(evidence: CapabilityEvidence, evidence_id: str) -> bool:
    issued = _PRODUCT_ISSUED_POSITIVE.get(id(evidence))
    return (
        issued is not None
        and issued[0]() is evidence
        and issued[1] == evidence_id
        and evidence.evidence_id == evidence_id
    )


@dataclass(frozen=True, slots=True)
class BetdaqAuthenticatedCapabilityIssuance:
    """One canonical BETDAQ authenticated-context capability observation."""

    profile: BookmakerCapabilityProfile
    evidence: CapabilityEvidence

    def __post_init__(self) -> None:
        if type(self.profile) is not BookmakerCapabilityProfile:
            raise CapabilityEvidenceError(
                "issuance profile must be exact BookmakerCapabilityProfile"
            )
        if type(self.evidence) is not CapabilityEvidence:
            raise CapabilityEvidenceError(
                "issuance evidence must be exact CapabilityEvidence"
            )
        if self.evidence.profile_id != self.profile.profile_id:
            raise CapabilityEvidenceError("issuance profile/evidence identity mismatch")


def _require_current_betdaq_issuance_surface() -> None:
    for name, expected, expected_code in _BETDAQ_CANONICAL_CLIENT_METHODS:
        current = getattr(_BETDAQ_CLIENT_TYPE, name, None)
        if (
            current is not expected
            or getattr(current, "__code__", None) is not expected_code
        ):
            raise CapabilityEvidenceError(
                "canonical BETDAQ account-evidence issuance surface changed"
            )
    if (
        _betdaq_account.BetdaqAccountReadOnlyClient is not _BETDAQ_CLIENT_TYPE
        or getattr(_betdaq_account, "_SECURE_ENDPOINT", None)
        != _BETDAQ_SECURE_ENDPOINT
        or _betdaq_account.BetdaqCredentials is not _BETDAQ_CREDENTIAL_TYPE
        or _betdaq_account.UrllibBetdaqSoapTransport is not _BETDAQ_TRANSPORT_TYPE
        or getattr(_betdaq_account, "_CANONICAL_HTTPS_POST", None)
        is not _BETDAQ_CANONICAL_HTTPS_POST
        or _BETDAQ_TRANSPORT_TYPE.post is not _BETDAQ_CANONICAL_HTTPS_POST
        or _BETDAQ_CANONICAL_HTTPS_POST.__code__
        is not _BETDAQ_CANONICAL_HTTPS_POST_CODE
        or _BETDAQ_CLIENT_TYPE.read_account_evidence is not _BETDAQ_READ_ACCOUNT_EVIDENCE
        or _BETDAQ_READ_ACCOUNT_EVIDENCE.__code__
        is not _BETDAQ_READ_ACCOUNT_EVIDENCE_CODE
        or getattr(_betdaq_account, "_require_canonical_account_transport", None)
        is not _BETDAQ_REQUIRE_CANONICAL_TRANSPORT
        or _BETDAQ_REQUIRE_CANONICAL_TRANSPORT.__code__
        is not _BETDAQ_REQUIRE_CANONICAL_TRANSPORT_CODE
        or getattr(_betdaq_account, "_authenticated_account_context", None)
        is not _BETDAQ_AUTHENTICATED_CONTEXT
        or _BETDAQ_AUTHENTICATED_CONTEXT.__code__
        is not _BETDAQ_AUTHENTICATED_CONTEXT_CODE
    ):
        raise CapabilityEvidenceError(
            "canonical BETDAQ account-evidence issuance surface changed"
        )


def _require_exact_betdaq_client_dispatch(client: object) -> None:
    for name, expected, _ in _BETDAQ_CANONICAL_CLIENT_METHODS:
        if name == "read_account_evidence":
            continue
        bound = getattr(client, name, None)
        if (
            getattr(bound, "__self__", None) is not client
            or getattr(bound, "__func__", None) is not expected
        ):
            raise CapabilityEvidenceError(
                "BETDAQ capability issuance client dispatch was replaced or shadowed"
            )


def issue_betdaq_authenticated_capability_evidence(
    client: object,
    capability: BookmakerCapability,
    *,
    committed_at: str,
    review_due_at: str,
    predecessor_id: str | None = None,
) -> BetdaqAuthenticatedCapabilityIssuance:
    """Issue bounded positive lifecycle evidence from a fresh canonical BETDAQ read.

    The upstream adapter proves only one live credential/application context. It does
    not prove a stable provider account identity or cross-session equivalence, so this
    issuer deliberately emits OBSERVED_AUTHENTICATED rather than
    OBSERVED_ACCOUNT_SCOPED evidence.
    """

    _require_current_betdaq_issuance_surface()
    if type(client) is not _BETDAQ_CLIENT_TYPE:
        raise CapabilityEvidenceError(
            "BETDAQ capability issuance requires exact canonical read-only client"
        )
    _require_exact_betdaq_client_dispatch(client)
    if getattr(client, "_venue_id", None) != _BETDAQ_VENUE_ID:
        raise CapabilityEvidenceError(
            "BETDAQ capability issuance requires the canonical betdaq venue"
        )
    credentials = getattr(client, "_credentials", None)
    if type(credentials) is not _BETDAQ_CREDENTIAL_TYPE:
        raise CapabilityEvidenceError(
            "BETDAQ capability issuance requires canonical credentials"
        )
    transport = getattr(client, "_transport", None)
    if type(transport) is not _BETDAQ_TRANSPORT_TYPE:
        raise CapabilityEvidenceError(
            "BETDAQ capability issuance requires canonical HTTPS transport"
        )
    bound_post = getattr(transport, "post", None)
    if (
        getattr(bound_post, "__self__", None) is not transport
        or getattr(bound_post, "__func__", None) is not _BETDAQ_CANONICAL_HTTPS_POST
    ):
        raise CapabilityEvidenceError(
            "BETDAQ capability issuance transport was replaced or shadowed"
        )
    if not isinstance(capability, BookmakerCapability):
        raise CapabilityEvidenceError("capability must be BookmakerCapability")
    try:
        acquisition = _BETDAQ_READ_ACCOUNT_EVIDENCE(
            client, frozenset({capability})
        )
    except _BETDAQ_ERROR_TYPE as exc:
        raise CapabilityEvidenceError(
            "canonical BETDAQ account capability acquisition failed"
        ) from exc
    if type(acquisition) is not _BETDAQ_ACCOUNT_EVIDENCE_TYPE:
        raise CapabilityEvidenceError(
            "BETDAQ account acquisition did not return canonical evidence"
        )
    context = acquisition.account_context
    if type(context) is not _BETDAQ_ACCOUNT_CONTEXT_TYPE:
        raise CapabilityEvidenceError(
            "BETDAQ account acquisition context is not canonical"
        )
    if (
        context.stable_account_identity_proven is not False
        or context.cross_session_equivalence_proven is not False
    ):
        raise CapabilityEvidenceError(
            "BETDAQ authenticated context overclaims stable account identity"
        )
    profile = acquisition.snapshot.profile
    if type(profile) is not BookmakerCapabilityProfile:
        raise CapabilityEvidenceError(
            "BETDAQ account acquisition profile is not canonical"
        )
    if (
        profile.venue_id != _BETDAQ_VENUE_ID
        or context.venue_id != _BETDAQ_VENUE_ID
        or profile.account_id != context.session_context_id
        or profile.adapter_id != _BETDAQ_ADAPTER_ID
        or profile.adapter_version != _BETDAQ_ADAPTER_VERSION
        or capability not in acquisition.snapshot.observed_capabilities
        or profile.state_of(capability) is not BookmakerCapabilityState.SUPPORTED
    ):
        raise CapabilityEvidenceError(
            "BETDAQ account acquisition does not prove exact requested capability"
        )
    evidence = CapabilityEvidence(
        profile_id=profile.profile_id,
        capability=capability,
        support_state=BookmakerCapabilityState.SUPPORTED,
        strength=CapabilityEvidenceStrength.OBSERVED_AUTHENTICATED,
        source=CapabilityEvidenceSource.AUTHENTICATED_OBSERVATION,
        scope=CapabilityScope(
            venue_id=_BETDAQ_VENUE_ID,
            account_id=None,
            environment="production",
            credential_identity=context.session_context_id,
        ),
        observed_at=profile.observed_at,
        committed_at=committed_at,
        review_due_at=review_due_at,
        validation_policy_version=BETDAQ_AUTHENTICATED_VALIDATION_POLICY_VERSION,
        source_contract_ref=BETDAQ_AUTHENTICATED_SOURCE_CONTRACT_REF,
        source_payload_sha256=profile.source_payload_sha256,
        predecessor_id=predecessor_id,
    )
    _remember_product_issued(evidence)
    return BetdaqAuthenticatedCapabilityIssuance(profile, evidence)


@dataclass(frozen=True, slots=True)
class CapabilityAvailability:
    evidence_id: str
    state: CapabilityAvailabilityState
    observed_at: str
    source_ref: str
    source_payload_sha256: str

    def __post_init__(self) -> None:
        _sha(self.evidence_id, "evidence_id")
        if not isinstance(self.state, CapabilityAvailabilityState):
            raise CapabilityEvidenceError("state must be CapabilityAvailabilityState")
        _time(self.observed_at, "observed_at")
        _text(self.source_ref, "source_ref")
        _sha(self.source_payload_sha256, "source_payload_sha256")

    def payload(self) -> dict[str, object]:
        return {
            "evidence_id": self.evidence_id,
            "state": self.state.value,
            "observed_at": self.observed_at,
            "source_ref": self.source_ref,
            "source_payload_sha256": self.source_payload_sha256,
        }

    @property
    def availability_id(self) -> str:
        return _hash(self.payload())


@dataclass(frozen=True, slots=True)
class CapabilityRequirement:
    capability: BookmakerCapability
    minimum_strength: CapabilityEvidenceStrength
    scope: CapabilityScope
    validation_policy_version: str
    source_contract_ref: str
    max_observation_age_seconds: int
    require_available: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.capability, BookmakerCapability):
            raise CapabilityEvidenceError("capability must be BookmakerCapability")
        if not isinstance(self.minimum_strength, CapabilityEvidenceStrength):
            raise CapabilityEvidenceError("minimum_strength must be CapabilityEvidenceStrength")
        if self.minimum_strength is CapabilityEvidenceStrength.UNPROVEN:
            raise CapabilityEvidenceError("UNPROVEN cannot authorize a capability")
        if type(self.scope) is not CapabilityScope:
            raise CapabilityEvidenceError("scope must be exact CapabilityScope")
        _text(self.validation_policy_version, "validation_policy_version")
        _text(self.source_contract_ref, "source_contract_ref")
        if (
            not isinstance(self.max_observation_age_seconds, int)
            or isinstance(self.max_observation_age_seconds, bool)
            or self.max_observation_age_seconds <= 0
        ):
            raise CapabilityEvidenceError(
                "max_observation_age_seconds must be a positive integer"
            )
        if not isinstance(self.require_available, bool):
            raise CapabilityEvidenceError("require_available must be bool")


@dataclass(frozen=True, slots=True)
class CapabilityDecision:
    allowed: bool
    lifecycle: CapabilityLifecycleState
    availability: CapabilityAvailabilityState
    evidence_id: str | None
    reason: str


def _scope_key(scope: CapabilityScope) -> tuple[object, ...]:
    # Credential identity is excluded so rotation finds old evidence and becomes
    # REVALIDATION_REQUIRED instead of looking like no prior evidence.
    return (
        scope.venue_id,
        scope.account_id,
        scope.environment,
        scope.jurisdiction,
        scope.sport,
        scope.market_family,
        scope.live_mode,
    )


class CapabilityEvidenceJournal:
    """Immutable lifecycle journal over existing canonical capability profiles."""

    def __init__(self) -> None:
        self._evidence: dict[str, CapabilityEvidence] = {}
        self._availability: dict[str, CapabilityAvailability] = {}

    def publish(self, evidence: CapabilityEvidence) -> str:
        if type(evidence) is not CapabilityEvidence:
            raise CapabilityEvidenceError("evidence must be exact CapabilityEvidence")
        evidence_id = evidence.evidence_id
        if evidence_id in self._evidence:
            return evidence_id
        same_scope: list[tuple[str, CapabilityEvidence]] = []
        for other_id, other in self._evidence.items():
            if (
                other.capability is evidence.capability
                and _scope_key(other.scope) == _scope_key(evidence.scope)
            ):
                same_scope.append((other_id, other))
                if (
                    _time(other.committed_at, "other.committed_at")
                    == _time(evidence.committed_at, "committed_at")
                    and other_id != evidence_id
                ):
                    raise CapabilityEvidenceError(
                        "conflicting evidence at the same commit timestamp"
                    )

        latest: tuple[str, CapabilityEvidence] | None = None
        if same_scope:
            latest = max(
                same_scope,
                key=lambda item: (
                    _time(item[1].committed_at, "committed_at"),
                    item[0],
                ),
            )
            if evidence.predecessor_id != latest[0]:
                raise CapabilityEvidenceError(
                    "successor evidence must link the exact latest predecessor"
                )
        elif evidence.predecessor_id is not None:
            raise CapabilityEvidenceError("first evidence cannot declare a predecessor")

        if evidence.predecessor_id is not None:
            predecessor = self._evidence.get(evidence.predecessor_id)
            if predecessor is None:
                raise CapabilityEvidenceError("predecessor is not in this journal")
            if (
                predecessor.capability is not evidence.capability
                or _scope_key(predecessor.scope) != _scope_key(evidence.scope)
            ):
                raise CapabilityEvidenceError("predecessor scope/capability mismatch")
            if _time(evidence.committed_at, "committed_at") <= _time(
                predecessor.committed_at, "predecessor.committed_at"
            ):
                raise CapabilityEvidenceError("revalidation must commit after predecessor")
            if _time(evidence.observed_at, "observed_at") <= _time(
                predecessor.observed_at, "predecessor.observed_at"
            ):
                raise CapabilityEvidenceError(
                    "successor evidence must be observed after predecessor"
                )
        self._evidence[evidence_id] = evidence
        return evidence_id

    def publish_availability(self, availability: CapabilityAvailability) -> str:
        if type(availability) is not CapabilityAvailability:
            raise CapabilityEvidenceError(
                "availability must be exact CapabilityAvailability"
            )
        evidence = self._evidence.get(availability.evidence_id)
        if evidence is None:
            raise CapabilityEvidenceError("availability references unknown evidence")
        if _time(availability.observed_at, "availability.observed_at") < _time(
            evidence.observed_at, "evidence.observed_at"
        ):
            raise CapabilityEvidenceError(
                "availability cannot precede its capability observation"
            )
        availability_id = availability.availability_id
        if availability_id in self._availability:
            return availability_id
        for other_id, other in self._availability.items():
            if (
                other.evidence_id == availability.evidence_id
                and _time(other.observed_at, "other.observed_at")
                == _time(availability.observed_at, "availability.observed_at")
                and other_id != availability_id
            ):
                raise CapabilityEvidenceError(
                    "conflicting availability at the same observation timestamp"
                )
        self._availability[availability_id] = availability
        return availability_id

    def resolve(
        self,
        requirement: CapabilityRequirement,
        profiles: Mapping[str, BookmakerCapabilityProfile],
        *,
        as_of: str,
    ) -> CapabilityDecision:
        if type(requirement) is not CapabilityRequirement:
            raise CapabilityEvidenceError("requirement must be exact CapabilityRequirement")
        decision_at = _time(as_of, "as_of")
        candidates = [
            (evidence_id, evidence)
            for evidence_id, evidence in self._evidence.items()
            if evidence.capability is requirement.capability
            and _scope_key(evidence.scope) == _scope_key(requirement.scope)
            and _time(evidence.committed_at, "committed_at") <= decision_at
        ]
        if not candidates:
            return CapabilityDecision(
                False,
                CapabilityLifecycleState.UNKNOWN,
                CapabilityAvailabilityState.UNKNOWN,
                None,
                "no committed evidence for exact capability scope",
            )
        evidence_id, evidence = max(
            candidates,
            key=lambda item: (_time(item[1].committed_at, "committed_at"), item[0]),
        )
        availability = self._latest_availability(evidence_id, decision_at)
        # AVAILABLE is a positive runtime observation. The public DTO records the
        # assertion for audit/restart, but cannot mint provider-health authority by
        # itself. Conservative negative/degraded states remain usable immediately.
        state = (
            CapabilityAvailabilityState.UNKNOWN
            if availability is None
            or availability.state is CapabilityAvailabilityState.AVAILABLE
            else availability.state
        )
        profile = profiles.get(evidence.profile_id)
        if type(profile) is not BookmakerCapabilityProfile:
            return CapabilityDecision(
                False,
                CapabilityLifecycleState.UNKNOWN,
                state,
                evidence_id,
                "canonical profile cannot be re-resolved",
            )
        return _evaluate(evidence, evidence_id, profile, requirement, decision_at, state)

    def _latest_availability(
        self, evidence_id: str, as_of: datetime
    ) -> CapabilityAvailability | None:
        items = [
            item
            for item in self._availability.values()
            if item.evidence_id == evidence_id
            and _time(item.observed_at, "availability.observed_at") <= as_of
        ]
        if not items:
            return None
        return max(
            items,
            key=lambda item: (
                _time(item.observed_at, "availability.observed_at"),
                item.availability_id,
            ),
        )

    def to_json(self) -> str:
        payload = {
            "schema_version": 1,
            "evidence": [
                evidence.payload()
                for _, evidence in sorted(self._evidence.items())
            ],
            "availability": [
                item.payload() for _, item in sorted(self._availability.items())
            ],
        }
        return json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )

    @classmethod
    def from_json(cls, payload: str) -> "CapabilityEvidenceJournal":
        try:
            raw = json.loads(
                _text(payload, "payload"),
                object_pairs_hook=_strict_json_object,
                parse_constant=_reject_nonstandard_json_constant,
            )
        except json.JSONDecodeError as exc:
            raise CapabilityEvidenceError("payload must be valid JSON") from exc
        if not isinstance(raw, dict):
            raise CapabilityEvidenceError("journal payload must be an object")
        if type(raw.get("schema_version")) is not int or raw.get("schema_version") != 1:
            raise CapabilityEvidenceError("unsupported journal schema")
        _require_exact_object_keys(
            raw,
            frozenset({"schema_version", "evidence", "availability"}),
            "journal",
        )
        raw_evidence = raw.get("evidence")
        raw_availability = raw.get("availability")
        if not isinstance(raw_evidence, list) or not isinstance(raw_availability, list):
            raise CapabilityEvidenceError("journal lists are malformed")

        pending = [_evidence_from_payload(item) for item in raw_evidence]
        evidence_ids = [evidence.evidence_id for evidence in pending]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise CapabilityEvidenceError("journal contains duplicate evidence entries")
        journal = cls()
        while pending:
            deferred: list[CapabilityEvidence] = []
            progress = False
            for evidence in pending:
                if (
                    evidence.predecessor_id is not None
                    and evidence.predecessor_id not in journal._evidence
                ):
                    deferred.append(evidence)
                    continue
                journal.publish(evidence)
                progress = True
            if not progress:
                raise CapabilityEvidenceError("missing or cyclic predecessor evidence")
            pending = deferred

        availability_items = [
            _availability_from_payload(item) for item in raw_availability
        ]
        availability_ids = [item.availability_id for item in availability_items]
        if len(availability_ids) != len(set(availability_ids)):
            raise CapabilityEvidenceError(
                "journal contains duplicate availability entries"
            )
        for item in availability_items:
            journal.publish_availability(item)
        return journal

    def staged_with_exact_evidence(
        self, evidence: CapabilityEvidence
    ) -> "CapabilityEvidenceJournal":
        """Return an isolated validation journal containing the exact candidate object.

        Durable JSON intentionally strips process-local positive provenance. This helper
        lets a fresh/product-issued in-process observation be evaluated against a durable
        predecessor chain without mutating that chain before qualification succeeds.
        If the same evidence id is already present, only an exactly identical payload may
        be replaced by the supplied object; newer same-scope successors remain present
        and continue to win normal latest-evidence resolution.
        """

        if type(evidence) is not CapabilityEvidence:
            raise CapabilityEvidenceError(
                "staged evidence must be exact CapabilityEvidence"
            )
        staged = CapabilityEvidenceJournal.from_json(self.to_json())
        evidence_id = evidence.evidence_id
        existing = staged._evidence.get(evidence_id)
        if existing is not None:
            if existing.payload() != evidence.payload():
                raise CapabilityEvidenceError(
                    "staged evidence id collides with different payload"
                )
            staged._evidence[evidence_id] = evidence
            return staged
        staged.publish(evidence)
        return staged


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise CapabilityEvidenceError("payload contains duplicate JSON object key")
        value[key] = item
    return value


def _reject_nonstandard_json_constant(value: str) -> object:
    raise CapabilityEvidenceError(
        f"payload contains non-standard JSON constant: {value}"
    )


def _require_exact_object_keys(
    raw: dict[str, object], expected: frozenset[str], field: str
) -> None:
    if frozenset(raw) != expected:
        raise CapabilityEvidenceError(
            f"{field} contains unexpected or missing fields"
        )


def _scope_from_payload(raw: object) -> CapabilityScope:
    if not isinstance(raw, dict):
        raise CapabilityEvidenceError("scope must be an object")
    _require_exact_object_keys(
        raw,
        frozenset(
            {
                "venue_id",
                "account_id",
                "environment",
                "jurisdiction",
                "sport",
                "market_family",
                "live_mode",
                "credential_identity",
            }
        ),
        "scope",
    )
    return CapabilityScope(
        venue_id=_text(raw.get("venue_id"), "venue_id"),
        account_id=_optional_text(raw.get("account_id"), "account_id"),
        environment=_text(raw.get("environment"), "environment"),
        jurisdiction=_optional_text(raw.get("jurisdiction"), "jurisdiction"),
        sport=_optional_text(raw.get("sport"), "sport"),
        market_family=_optional_text(raw.get("market_family"), "market_family"),
        live_mode=_optional_text(raw.get("live_mode"), "live_mode"),
        credential_identity=_optional_text(
            raw.get("credential_identity"), "credential_identity"
        ),
    )


def _evidence_from_payload(raw: object) -> CapabilityEvidence:
    if not isinstance(raw, dict):
        raise CapabilityEvidenceError("evidence must be an object")
    _require_exact_object_keys(
        raw,
        frozenset(
            {
                "profile_id",
                "capability",
                "support_state",
                "strength",
                "source",
                "scope",
                "observed_at",
                "committed_at",
                "review_due_at",
                "validation_policy_version",
                "source_contract_ref",
                "source_payload_sha256",
                "provider_expires_at",
                "predecessor_id",
            }
        ),
        "evidence",
    )
    try:
        capability = BookmakerCapability(_text(raw.get("capability"), "capability"))
        support = BookmakerCapabilityState(
            _text(raw.get("support_state"), "support_state")
        )
        strength = CapabilityEvidenceStrength[
            _text(raw.get("strength"), "strength")
        ]
        source = CapabilityEvidenceSource(_text(raw.get("source"), "source"))
    except (KeyError, ValueError) as exc:
        raise CapabilityEvidenceError("invalid evidence enum") from exc
    return CapabilityEvidence(
        profile_id=_sha(raw.get("profile_id"), "profile_id"),
        capability=capability,
        support_state=support,
        strength=strength,
        source=source,
        scope=_scope_from_payload(raw.get("scope")),
        observed_at=_text(raw.get("observed_at"), "observed_at"),
        committed_at=_text(raw.get("committed_at"), "committed_at"),
        review_due_at=_text(raw.get("review_due_at"), "review_due_at"),
        validation_policy_version=_text(
            raw.get("validation_policy_version"), "validation_policy_version"
        ),
        source_contract_ref=_text(raw.get("source_contract_ref"), "source_contract_ref"),
        source_payload_sha256=_sha(
            raw.get("source_payload_sha256"), "source_payload_sha256"
        ),
        provider_expires_at=_optional_text(
            raw.get("provider_expires_at"), "provider_expires_at"
        ),
        predecessor_id=_optional_text(raw.get("predecessor_id"), "predecessor_id"),
    )


def _availability_from_payload(raw: object) -> CapabilityAvailability:
    if not isinstance(raw, dict):
        raise CapabilityEvidenceError("availability must be an object")
    _require_exact_object_keys(
        raw,
        frozenset(
            {
                "evidence_id",
                "state",
                "observed_at",
                "source_ref",
                "source_payload_sha256",
            }
        ),
        "availability",
    )
    try:
        state = CapabilityAvailabilityState(_text(raw.get("state"), "state"))
    except ValueError as exc:
        raise CapabilityEvidenceError("invalid availability state") from exc
    return CapabilityAvailability(
        evidence_id=_sha(raw.get("evidence_id"), "evidence_id"),
        state=state,
        observed_at=_text(raw.get("observed_at"), "observed_at"),
        source_ref=_text(raw.get("source_ref"), "source_ref"),
        source_payload_sha256=_sha(
            raw.get("source_payload_sha256"), "source_payload_sha256"
        ),
    )


def _evaluate(
    evidence: CapabilityEvidence,
    evidence_id: str,
    profile: BookmakerCapabilityProfile,
    requirement: CapabilityRequirement,
    as_of: datetime,
    availability: CapabilityAvailabilityState,
) -> CapabilityDecision:
    def deny(lifecycle: CapabilityLifecycleState, reason: str) -> CapabilityDecision:
        return CapabilityDecision(False, lifecycle, availability, evidence_id, reason)

    if profile.profile_id != evidence.profile_id:
        return deny(CapabilityLifecycleState.UNKNOWN, "profile identity mismatch")
    if profile.venue_id != evidence.scope.venue_id:
        return deny(CapabilityLifecycleState.UNKNOWN, "profile venue mismatch")
    if evidence.scope.account_id is not None:
        if profile.account_id != evidence.scope.account_id:
            return deny(CapabilityLifecycleState.UNKNOWN, "profile account mismatch")
    if profile.state_of(evidence.capability) is not evidence.support_state:
        return deny(CapabilityLifecycleState.UNKNOWN, "profile support mismatch")
    profile_observed_at = _time(profile.observed_at, "profile.observed_at")
    if profile_observed_at != _time(evidence.observed_at, "evidence.observed_at"):
        return deny(CapabilityLifecycleState.UNKNOWN, "observation identity mismatch")
    if (
        evidence.strength is CapabilityEvidenceStrength.OBSERVED_AUTHENTICATED
        and evidence.validation_policy_version
        == BETDAQ_AUTHENTICATED_VALIDATION_POLICY_VERSION
        and evidence.source_contract_ref == BETDAQ_AUTHENTICATED_SOURCE_CONTRACT_REF
        and requirement.max_observation_age_seconds
        > BETDAQ_AUTHENTICATED_MAX_OBSERVATION_AGE_SECONDS
    ):
        return deny(
            CapabilityLifecycleState.REVALIDATION_REQUIRED,
            "requested observation age exceeds BETDAQ product policy",
        )
    observation_age = (as_of - profile_observed_at).total_seconds()
    if observation_age >= requirement.max_observation_age_seconds:
        return deny(
            CapabilityLifecycleState.REVALIDATION_REQUIRED,
            "canonical observation age exceeds policy",
        )
    if evidence.scope.credential_identity != requirement.scope.credential_identity:
        return deny(
            CapabilityLifecycleState.REVALIDATION_REQUIRED,
            "credential identity changed",
        )
    if evidence.validation_policy_version != requirement.validation_policy_version:
        return deny(CapabilityLifecycleState.REVALIDATION_REQUIRED, "policy changed")
    if evidence.source_contract_ref != requirement.source_contract_ref:
        return deny(CapabilityLifecycleState.REVALIDATION_REQUIRED, "contract changed")
    if evidence.provider_expires_at is not None:
        if as_of >= _time(evidence.provider_expires_at, "provider_expires_at"):
            return deny(CapabilityLifecycleState.STALE, "provider evidence expired")
    if as_of >= _time(evidence.review_due_at, "review_due_at"):
        return deny(CapabilityLifecycleState.REVALIDATION_REQUIRED, "review is due")
    if evidence.support_state is not BookmakerCapabilityState.SUPPORTED:
        return deny(
            CapabilityLifecycleState.REVOKED_OR_UNSUPPORTED,
            "latest evidence is unsupported/revoked",
        )
    if requirement.minimum_strength is CapabilityEvidenceStrength.EXECUTION_PROVEN:
        return deny(
            CapabilityLifecycleState.UNKNOWN,
            "execution proof requires separate real execution authority",
        )
    if evidence.strength < requirement.minimum_strength:
        return deny(CapabilityLifecycleState.CURRENT, "evidence strength is too weak")
    if evidence.strength in {
        CapabilityEvidenceStrength.DOCUMENTED_ONLY,
        CapabilityEvidenceStrength.OBSERVED_PUBLIC,
    }:
        return deny(
            CapabilityLifecycleState.UNKNOWN,
            "document/public observation requires product-owned provenance authority",
        )
    if evidence.strength is CapabilityEvidenceStrength.OBSERVED_ACCOUNT_SCOPED:
        return deny(
            CapabilityLifecycleState.REVALIDATION_REQUIRED,
            "authenticated/account observation requires product-owned upstream authority; stable account-scoped authority is not proven",
        )
    if evidence.strength is CapabilityEvidenceStrength.OBSERVED_AUTHENTICATED:
        if not _is_product_issued(evidence, evidence_id):
            return deny(
                CapabilityLifecycleState.REVALIDATION_REQUIRED,
                "authenticated observation requires product-owned upstream authority",
            )
    if requirement.require_available:
        if availability is not CapabilityAvailabilityState.AVAILABLE:
            return deny(CapabilityLifecycleState.CURRENT, "provider is not AVAILABLE")
    return CapabilityDecision(
        True,
        CapabilityLifecycleState.CURRENT,
        availability,
        evidence_id,
        (
            "current product-issued authenticated capability evidence"
            if evidence.strength is CapabilityEvidenceStrength.OBSERVED_AUTHENTICATED
            else "current exact-scope capability evidence"
        ),
    )
