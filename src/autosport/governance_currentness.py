"""Current-only qualification for durable bookmaker governance evidence.

This module does not grant execution authority. It classifies whether durable
``BookmakerGovernanceEvidence`` is usable as bounded *current* decision evidence
under an explicit freshness policy.

The underlying registry does not persist a product-owned registration/availability
instant for governance records. Consequently a caller-selected historical cutoff
cannot prove that a record was already present at that cutoff: a later registry
append could carry an older caller-supplied ``observed_at``. Historical resolution
therefore fails closed. Positive currentness is available only through
``resolve_current_governance()``, whose cutoff is sampled from the product clock at
resolution time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import math
import time

from . import bookmaker_capability_registry as _registry_module
from .bookmaker_capability_registry import (
    BookmakerCapabilityRegistry,
    BookmakerGovernanceEvidence,
    GovernancePermissionState,
)


class GovernanceCurrentnessError(ValueError):
    """Raised when currentness inputs are malformed or authority is substituted."""


class GovernanceEvidenceState(str, Enum):
    """Bounded evidence interpretation; never execution permission."""

    UNKNOWN = "unknown"
    SUPPORTS_PERMITTED = "supports_permitted"
    SUPPORTS_PROHIBITED = "supports_prohibited"


class GovernanceCurrentnessReason(str, Enum):
    NO_ELIGIBLE_EVIDENCE = "no_eligible_evidence"
    STALE = "stale"
    AMBIGUOUS_LATEST = "ambiguous_latest"
    TERMS_DOCUMENT_CONFLICT = "terms_document_conflict"
    TERMS_PERMISSION_CONFLICT = "terms_permission_conflict"
    RECORDED_UNKNOWN = "recorded_unknown"
    HISTORICAL_AVAILABILITY_UNPROVEN = "historical_availability_unproven"
    QUALIFIED_PERMITTED = "qualified_permitted"
    QUALIFIED_PROHIBITED = "qualified_prohibited"


def _timestamp(value: str, field: str) -> datetime:
    if type(value) is not str or not value or value != value.strip():
        raise GovernanceCurrentnessError(f"{field} must be a non-empty trimmed string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise GovernanceCurrentnessError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise GovernanceCurrentnessError(
            f"{field} must include a timezone offset"
        )
    return parsed


def _text(value: str, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise GovernanceCurrentnessError(f"{field} must be a non-empty trimmed string")
    return value


def _validate_request(
    registry: BookmakerCapabilityRegistry,
    *,
    venue_id: str,
    account_id: str,
    jurisdiction: str,
    max_age_seconds: int,
) -> None:
    if type(registry) is not BookmakerCapabilityRegistry:
        raise GovernanceCurrentnessError(
            "registry must be the exact durable BookmakerCapabilityRegistry"
        )
    _text(venue_id, "venue_id")
    _text(account_id, "account_id")
    _text(jurisdiction, "jurisdiction")
    if type(max_age_seconds) is not int or max_age_seconds <= 0:
        raise GovernanceCurrentnessError(
            "max_age_seconds must be a positive integer"
        )


@dataclass(frozen=True, slots=True)
class GovernanceDecisionEvidence:
    venue_id: str
    account_id: str
    jurisdiction: str
    decision_at: str
    max_age_seconds: int
    state: GovernanceEvidenceState
    reason: GovernanceCurrentnessReason
    evidence_id: str | None
    terms_version: str | None
    source_payload_sha256: str | None
    evidence_age_seconds: float | None
    execution_authorized: bool = False

    def __post_init__(self) -> None:
        _text(self.venue_id, "venue_id")
        _text(self.account_id, "account_id")
        _text(self.jurisdiction, "jurisdiction")
        _timestamp(self.decision_at, "decision_at")
        if type(self.max_age_seconds) is not int or self.max_age_seconds <= 0:
            raise GovernanceCurrentnessError(
                "max_age_seconds must be a positive integer"
            )
        if not isinstance(self.state, GovernanceEvidenceState):
            raise GovernanceCurrentnessError(
                "state must be a GovernanceEvidenceState"
            )
        if not isinstance(self.reason, GovernanceCurrentnessReason):
            raise GovernanceCurrentnessError(
                "reason must be a GovernanceCurrentnessReason"
            )
        if self.evidence_id is not None:
            if (
                type(self.evidence_id) is not str
                or len(self.evidence_id) != 64
                or any(char not in "0123456789abcdef" for char in self.evidence_id)
            ):
                raise GovernanceCurrentnessError(
                    "evidence_id must be a canonical SHA-256 digest"
                )
        if self.terms_version is not None:
            _text(self.terms_version, "terms_version")
        if self.source_payload_sha256 is not None:
            if (
                type(self.source_payload_sha256) is not str
                or len(self.source_payload_sha256) != 64
                or any(
                    char not in "0123456789abcdef"
                    for char in self.source_payload_sha256
                )
            ):
                raise GovernanceCurrentnessError(
                    "source_payload_sha256 must be a canonical SHA-256 digest"
                )
        if self.evidence_age_seconds is not None:
            if (
                type(self.evidence_age_seconds) is not float
                or not math.isfinite(self.evidence_age_seconds)
                or self.evidence_age_seconds < 0.0
            ):
                raise GovernanceCurrentnessError(
                    "evidence_age_seconds must be a finite non-negative float"
                )
        if self.execution_authorized is not False:
            raise GovernanceCurrentnessError(
                "governance currentness evidence cannot authorize execution"
            )


def _canonical_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _age_microseconds(observed_at: datetime, decision_at: datetime) -> int:
    age = decision_at - observed_at
    return ((age.days * 24 * 60 * 60) + age.seconds) * 1_000_000 + age.microseconds


def _freshness_limit_exceeded(
    observed_at: datetime,
    decision_at: datetime,
    max_age_seconds: int,
) -> bool:
    """Pure exact freshness arithmetic; it grants no governance authority."""

    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise GovernanceCurrentnessError("observed_at must be timezone-aware")
    if decision_at.tzinfo is None or decision_at.utcoffset() is None:
        raise GovernanceCurrentnessError("decision_at must be timezone-aware")
    if type(max_age_seconds) is not int or max_age_seconds <= 0:
        raise GovernanceCurrentnessError(
            "max_age_seconds must be a positive integer"
        )
    return _age_microseconds(observed_at, decision_at) > max_age_seconds * 1_000_000


def _result(
    *,
    venue_id: str,
    account_id: str,
    jurisdiction: str,
    decision_at: str,
    max_age_seconds: int,
    state: GovernanceEvidenceState,
    reason: GovernanceCurrentnessReason,
    evidence: BookmakerGovernanceEvidence | None = None,
    evidence_age_seconds: float | None = None,
) -> GovernanceDecisionEvidence:
    return GovernanceDecisionEvidence(
        venue_id=venue_id,
        account_id=account_id,
        jurisdiction=jurisdiction,
        decision_at=decision_at,
        max_age_seconds=max_age_seconds,
        state=state,
        reason=reason,
        evidence_id=evidence.evidence_id if evidence is not None else None,
        terms_version=evidence.terms_version if evidence is not None else None,
        source_payload_sha256=(
            evidence.source_payload_sha256 if evidence is not None else None
        ),
        evidence_age_seconds=evidence_age_seconds,
        execution_authorized=False,
    )


def resolve_governance_for_decision(
    registry: BookmakerCapabilityRegistry,
    *,
    venue_id: str,
    account_id: str,
    jurisdiction: str,
    decision_at: str,
    max_age_seconds: int,
) -> GovernanceDecisionEvidence:
    """Fail closed for caller-selected historical decision cutoffs.

    ``BookmakerCapabilityRegistry`` currently proves durable content integrity, but
    its governance entries do not carry a product-owned registration/availability
    timestamp. ``observed_at`` is source observation metadata and can be older than
    the append that made the evidence available to Autosport. Therefore no positive
    or negative historical currentness claim may be reconstructed from that timestamp
    alone.
    """

    _validate_request(
        registry,
        venue_id=venue_id,
        account_id=account_id,
        jurisdiction=jurisdiction,
        max_age_seconds=max_age_seconds,
    )
    parsed = _timestamp(decision_at, "decision_at")
    return _result(
        venue_id=venue_id,
        account_id=account_id,
        jurisdiction=jurisdiction,
        decision_at=_canonical_utc(parsed),
        max_age_seconds=max_age_seconds,
        state=GovernanceEvidenceState.UNKNOWN,
        reason=GovernanceCurrentnessReason.HISTORICAL_AVAILABILITY_UNPROVEN,
    )


def _product_time_ns() -> int:
    """Sample only the canonical built-in time.time_ns product wall clock."""

    canonical_time = __import__("time")
    if canonical_time is not time:
        raise GovernanceCurrentnessError("product clock module authority changed")

    clock = getattr(canonical_time, "time_ns", None)
    if (
        type(clock) is not type(abs)
        or getattr(clock, "__name__", None) != "time_ns"
        or getattr(clock, "__module__", None) != "time"
        or getattr(clock, "__self__", None) is not canonical_time
    ):
        raise GovernanceCurrentnessError(
            "product clock callable authority changed"
        )

    now_ns = clock()
    if type(now_ns) is not int or now_ns < 0:
        raise GovernanceCurrentnessError("product clock returned an invalid value")
    return now_ns


def _global_witnesses(function: object) -> tuple[tuple[dict[str, object], str, object], ...]:
    """Capture global bindings actually referenced by one canonical Python function."""

    code = getattr(function, "__code__", None)
    namespace = getattr(function, "__globals__", None)
    if code is None or type(namespace) is not dict:
        return ()
    return tuple(
        (namespace, name, namespace[name])
        for name in code.co_names
        if name in namespace
    )


# These are read/clock dispatch witnesses, not alternate authorities or stores. The
# current resolver checks their public dispatch points before invoking the captured
# canonical callables, so class/module rebinding cannot redirect positive currentness
# issuance after validation.
_CANONICAL_PRODUCT_TIME_NS = _product_time_ns
_CANONICAL_GOVERNANCE_HISTORY = BookmakerCapabilityRegistry.governance_history
_CANONICAL_REGISTRY_READ_SURFACES = (
    ("governance_history", BookmakerCapabilityRegistry.governance_history),
    ("_load_document", BookmakerCapabilityRegistry._load_document),
    ("_profiles_from_document", BookmakerCapabilityRegistry._profiles_from_document),
    ("_decode_profile", BookmakerCapabilityRegistry._decode_profile),
    ("_profile_key", BookmakerCapabilityRegistry._profile_key),
    ("_governance_from_document", BookmakerCapabilityRegistry._governance_from_document),
    ("_governance_key", BookmakerCapabilityRegistry._governance_key),
    ("_decode_governance", BookmakerCapabilityRegistry._decode_governance),
)
_CANONICAL_REGISTRY_READ_CODES = tuple(
    (name, function, getattr(function, "__code__", None))
    for name, function in _CANONICAL_REGISTRY_READ_SURFACES
)
_CANONICAL_REGISTRY_DATA_SURFACES = (
    (
        _registry_module.BookmakerCapabilityFact,
        "__post_init__",
        _registry_module.BookmakerCapabilityFact.__post_init__,
    ),
    (
        _registry_module.BookmakerCapabilityProfile,
        "__post_init__",
        _registry_module.BookmakerCapabilityProfile.__post_init__,
    ),
    (
        _registry_module.BookmakerCapabilityProfile,
        "profile_id",
        _registry_module.BookmakerCapabilityProfile.profile_id,
    ),
    (
        _registry_module.BookmakerCapabilityProfile,
        "to_canonical_dict",
        _registry_module.BookmakerCapabilityProfile.to_canonical_dict,
    ),
    (
        BookmakerGovernanceEvidence,
        "__post_init__",
        BookmakerGovernanceEvidence.__post_init__,
    ),
    (
        BookmakerGovernanceEvidence,
        "evidence_id",
        BookmakerGovernanceEvidence.evidence_id,
    ),
    (
        BookmakerGovernanceEvidence,
        "to_canonical_dict",
        BookmakerGovernanceEvidence.to_canonical_dict,
    ),
)
_CANONICAL_REGISTRY_DEPENDENCY_GLOBALS = tuple(
    witness
    for _name, function in _CANONICAL_REGISTRY_READ_SURFACES
    for witness in _global_witnesses(function)
) + tuple(
    witness
    for _owner, _name, surface in _CANONICAL_REGISTRY_DATA_SURFACES
    for target in (
        surface.fget if isinstance(surface, property) else surface,
    )
    for witness in _global_witnesses(target)
)
_CANONICAL_REGISTRY_MODULE = _registry_module
_CANONICAL_REGISTRY_SCHEMA_VERSION = BookmakerCapabilityRegistry.SCHEMA_VERSION
_CANONICAL_STRICT_JSON_LOADS = _registry_module.strict_json_loads
_CANONICAL_STRICT_JSON_LOADS_CODE = getattr(_CANONICAL_STRICT_JSON_LOADS, "__code__", None)


def _require_registry_read_dispatch(registry: BookmakerCapabilityRegistry) -> None:
    """Seal the existing durable governance reader graph before positive use."""

    if type(registry) is not BookmakerCapabilityRegistry:
        raise GovernanceCurrentnessError(
            "registry must be the exact durable BookmakerCapabilityRegistry"
        )
    instance_state = vars(registry)
    for name, function, expected_code in _CANONICAL_REGISTRY_READ_CODES:
        if name in instance_state:
            raise GovernanceCurrentnessError(
                "governance registry reader dispatch authority changed"
            )
        live = getattr(BookmakerCapabilityRegistry, name, None)
        if live is not function or getattr(function, "__code__", None) is not expected_code:
            raise GovernanceCurrentnessError(
                "governance registry reader dispatch authority changed"
            )
    for owner, name, expected in _CANONICAL_REGISTRY_DATA_SURFACES:
        if getattr(owner, name, None) is not expected:
            raise GovernanceCurrentnessError(
                "governance registry data dependency authority changed"
            )
    for namespace, name, expected in _CANONICAL_REGISTRY_DEPENDENCY_GLOBALS:
        if namespace.get(name) is not expected:
            raise GovernanceCurrentnessError(
                "governance registry global dependency authority changed"
            )
    if (
        _registry_module is not _CANONICAL_REGISTRY_MODULE
        or BookmakerCapabilityRegistry.SCHEMA_VERSION != _CANONICAL_REGISTRY_SCHEMA_VERSION
        or _registry_module.strict_json_loads is not _CANONICAL_STRICT_JSON_LOADS
        or getattr(_CANONICAL_STRICT_JSON_LOADS, "__code__", None)
        is not _CANONICAL_STRICT_JSON_LOADS_CODE
        or _registry_module.BookmakerGovernanceEvidence is not BookmakerGovernanceEvidence
        or _registry_module.GovernancePermissionState is not GovernancePermissionState
    ):
        raise GovernanceCurrentnessError(
            "governance registry reader dependency authority changed"
        )


def resolve_current_governance(
    registry: BookmakerCapabilityRegistry,
    *,
    venue_id: str,
    account_id: str,
    jurisdiction: str,
    max_age_seconds: int,
) -> GovernanceDecisionEvidence:
    """Qualify durable governance evidence for the current product instant only."""

    _validate_request(
        registry,
        venue_id=venue_id,
        account_id=account_id,
        jurisdiction=jurisdiction,
        max_age_seconds=max_age_seconds,
    )
    if _product_time_ns is not _CANONICAL_PRODUCT_TIME_NS:
        raise GovernanceCurrentnessError(
            "product clock dispatch authority changed"
        )
    if (
        BookmakerCapabilityRegistry.governance_history
        is not _CANONICAL_GOVERNANCE_HISTORY
        or "governance_history" in vars(registry)
    ):
        raise GovernanceCurrentnessError(
            "governance history dispatch authority changed"
        )
    _require_registry_read_dispatch(registry)

    now_ns = _CANONICAL_PRODUCT_TIME_NS()
    seconds, remainder_ns = divmod(now_ns, 1_000_000_000)
    decision_time = datetime.fromtimestamp(seconds, tz=timezone.utc).replace(
        microsecond=remainder_ns // 1_000
    )
    decision_at = _canonical_utc(decision_time)

    history = _CANONICAL_GOVERNANCE_HISTORY(registry, venue_id, account_id)
    _require_registry_read_dispatch(registry)
    if any(type(item) is not BookmakerGovernanceEvidence for item in history):
        raise GovernanceCurrentnessError(
            "registry returned non-canonical governance evidence"
        )

    scoped = tuple(item for item in history if item.jurisdiction == jurisdiction)
    eligible = tuple(
        item
        for item in scoped
        if _timestamp(item.observed_at, "observed_at") <= decision_time
    )
    if not eligible:
        return _result(
            venue_id=venue_id,
            account_id=account_id,
            jurisdiction=jurisdiction,
            decision_at=decision_at,
            max_age_seconds=max_age_seconds,
            state=GovernanceEvidenceState.UNKNOWN,
            reason=GovernanceCurrentnessReason.NO_ELIGIBLE_EVIDENCE,
        )

    latest_time = max(
        _timestamp(item.observed_at, "observed_at") for item in eligible
    )
    latest = tuple(
        item
        for item in eligible
        if _timestamp(item.observed_at, "observed_at") == latest_time
    )
    if len(latest) != 1:
        return _result(
            venue_id=venue_id,
            account_id=account_id,
            jurisdiction=jurisdiction,
            decision_at=decision_at,
            max_age_seconds=max_age_seconds,
            state=GovernanceEvidenceState.UNKNOWN,
            reason=GovernanceCurrentnessReason.AMBIGUOUS_LATEST,
        )

    candidate = latest[0]
    age = decision_time - latest_time
    age_seconds = float(age.total_seconds())

    same_terms_digests = {
        item.source_payload_sha256
        for item in eligible
        if item.terms_version == candidate.terms_version
    }
    if len(same_terms_digests) != 1:
        return _result(
            venue_id=venue_id,
            account_id=account_id,
            jurisdiction=jurisdiction,
            decision_at=decision_at,
            max_age_seconds=max_age_seconds,
            state=GovernanceEvidenceState.UNKNOWN,
            reason=GovernanceCurrentnessReason.TERMS_DOCUMENT_CONFLICT,
            evidence=candidate,
            evidence_age_seconds=age_seconds,
        )

    same_document_permissions = {
        item.automation_permission
        for item in eligible
        if (
            item.terms_version == candidate.terms_version
            and item.source_payload_sha256 == candidate.source_payload_sha256
        )
    }
    if len(same_document_permissions) != 1:
        return _result(
            venue_id=venue_id,
            account_id=account_id,
            jurisdiction=jurisdiction,
            decision_at=decision_at,
            max_age_seconds=max_age_seconds,
            state=GovernanceEvidenceState.UNKNOWN,
            reason=GovernanceCurrentnessReason.TERMS_PERMISSION_CONFLICT,
            evidence=candidate,
            evidence_age_seconds=age_seconds,
        )

    if _freshness_limit_exceeded(latest_time, decision_time, max_age_seconds):
        return _result(
            venue_id=venue_id,
            account_id=account_id,
            jurisdiction=jurisdiction,
            decision_at=decision_at,
            max_age_seconds=max_age_seconds,
            state=GovernanceEvidenceState.UNKNOWN,
            reason=GovernanceCurrentnessReason.STALE,
            evidence=candidate,
            evidence_age_seconds=age_seconds,
        )

    if candidate.automation_permission is GovernancePermissionState.UNKNOWN:
        state = GovernanceEvidenceState.UNKNOWN
        reason = GovernanceCurrentnessReason.RECORDED_UNKNOWN
    elif candidate.automation_permission is GovernancePermissionState.PERMITTED:
        state = GovernanceEvidenceState.SUPPORTS_PERMITTED
        reason = GovernanceCurrentnessReason.QUALIFIED_PERMITTED
    elif candidate.automation_permission is GovernancePermissionState.PROHIBITED:
        state = GovernanceEvidenceState.SUPPORTS_PROHIBITED
        reason = GovernanceCurrentnessReason.QUALIFIED_PROHIBITED
    else:
        raise GovernanceCurrentnessError(
            "unsupported governance permission state"
        )

    return _result(
        venue_id=venue_id,
        account_id=account_id,
        jurisdiction=jurisdiction,
        decision_at=decision_at,
        max_age_seconds=max_age_seconds,
        state=state,
        reason=reason,
        evidence=candidate,
        evidence_age_seconds=age_seconds,
    )
