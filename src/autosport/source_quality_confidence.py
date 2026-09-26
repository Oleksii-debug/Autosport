from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
import re
from typing import Iterable

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class SourceClass(str, Enum):
    OFFICIAL_API = "OFFICIAL_API"
    BROWSER_ADAPTER = "BROWSER_ADAPTER"
    MANUAL = "MANUAL"


class ConfidenceAction(str, Enum):
    ACCEPT = "ACCEPT"
    DOWNWEIGHT = "DOWNWEIGHT"
    ABSTAIN = "ABSTAIN"


@dataclass(frozen=True, slots=True)
class SourceQualityPolicy:
    """Deterministic thresholds for one source-quality assessment boundary.

    ``accept_confidence`` is retained as a forward-compatible policy field.  The
    current implementation intentionally has no path to ``ACCEPT``: Autosport's
    canonical bookmaker capability record is caller-constructible and therefore
    is not, by itself, proof that an observation was issued by authenticated,
    durable product-owned provider evidence.
    """

    max_age: timedelta
    accept_confidence: Decimal = Decimal("0.80")
    downweight_confidence: Decimal = Decimal("0.50")
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.max_age, timedelta) or self.max_age <= timedelta(0):
            raise ValueError("max_age must be a positive timedelta")
        _validate_probability(self.accept_confidence, "accept_confidence")
        _validate_probability(self.downweight_confidence, "downweight_confidence")
        if self.downweight_confidence > self.accept_confidence:
            raise ValueError("downweight_confidence must be <= accept_confidence")
        if type(self.schema_version) is not int or self.schema_version < 1:
            raise ValueError("schema_version must be a positive exact int")


@dataclass(frozen=True, slots=True)
class SourceQualityObservation:
    """Caller-supplied source evidence, never an authority token.

    In particular, ``source_class=OFFICIAL_API`` is an assertion to be checked by
    a product-owned resolver in a future integration.  It is not sufficient to
    mint trusted/accepted evidence.
    """

    provider_id: str
    source_id: str
    evidence_id: str
    source_class: SourceClass
    observed_at: datetime
    base_confidence: Decimal
    schema_version: int
    transport_verified: bool
    provenance_bound: bool
    provenance_sha256: str
    corroborator_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider_id", _strict_text(self.provider_id, "provider_id"))
        object.__setattr__(self, "source_id", _strict_text(self.source_id, "source_id"))
        object.__setattr__(self, "evidence_id", _strict_text(self.evidence_id, "evidence_id"))
        if not isinstance(self.source_class, SourceClass):
            raise ValueError("source_class must be SourceClass")
        _validate_aware(self.observed_at, "observed_at")
        _validate_probability(self.base_confidence, "base_confidence")
        if type(self.schema_version) is not int or self.schema_version < 1:
            raise ValueError("schema_version must be a positive exact int")
        if type(self.transport_verified) is not bool:
            raise ValueError("transport_verified must be bool")
        if type(self.provenance_bound) is not bool:
            raise ValueError("provenance_bound must be bool")

        digest = _strict_text(self.provenance_sha256, "provenance_sha256").lower()
        if not _SHA256_RE.fullmatch(digest):
            raise ValueError("provenance_sha256 must be 64 hex chars")
        object.__setattr__(self, "provenance_sha256", digest)

        if type(self.corroborator_ids) is not tuple:
            raise ValueError("corroborator_ids must be an exact tuple")
        normalized = tuple(_strict_text(value, "corroborator_id") for value in self.corroborator_ids)
        if len(set(normalized)) != len(normalized):
            raise ValueError("duplicate corroborator_id")
        forbidden = {self.provider_id, self.source_id, self.evidence_id}
        if any(value in forbidden for value in normalized):
            raise ValueError("self-corroboration is not allowed")
        object.__setattr__(self, "corroborator_ids", normalized)


@dataclass(frozen=True, slots=True)
class SourceQualityAssessment:
    """Fail-closed result value without implicit numeric weighting authority.

    ``input_confidence`` preserves the caller/evidence confidence exactly; it is
    deliberately not named or presented as an already-weighted confidence.  The
    policy result is ``action``.  ``ACCEPT`` and authority-bearing corroboration
    remain structurally unavailable until canonical product-owned resolvers are
    integrated.
    """

    action: ConfidenceAction
    input_confidence: Decimal
    reasons: tuple[str, ...]
    corroborated: bool

    def __post_init__(self) -> None:
        if not isinstance(self.action, ConfidenceAction):
            raise ValueError("action must be ConfidenceAction")
        _validate_probability(self.input_confidence, "input_confidence")
        if type(self.reasons) is not tuple:
            raise ValueError("reasons must be an exact tuple")
        normalized_reasons = tuple(_strict_text(reason, "reason") for reason in self.reasons)
        object.__setattr__(self, "reasons", normalized_reasons)
        if type(self.corroborated) is not bool:
            raise ValueError("corroborated must be bool")
        if self.action is ConfidenceAction.ACCEPT:
            raise ValueError("ACCEPT requires canonical provider authority integration")
        if self.corroborated:
            raise ValueError("corroborated=True requires canonical corroborator authority integration")


def assess_source_quality(
    observation: SourceQualityObservation,
    *,
    now: datetime,
    policy: SourceQualityPolicy,
) -> SourceQualityAssessment:
    """Assess one observation without allowing caller-minted authority.

    Hard evidence failures always ``ABSTAIN``.  Caller-supplied corroborator IDs
    are diagnostic identities only and never mint a positive corroboration truth
    value.  Until authenticated, durable, product-owned provider/corroborator
    resolvers are wired into this boundary, ``OFFICIAL_API`` also always
    ``ABSTAIN``: a source-class label is not proof of official issuance.
    Browser/manual evidence can be used only as bounded ``DOWNWEIGHT`` evidence.
    The returned numeric field is explicitly the input confidence, not a derived
    or already-weighted trust score; downstream consumers must respect ``action``.
    """

    if not isinstance(observation, SourceQualityObservation):
        raise TypeError("observation must be SourceQualityObservation")
    _validate_aware(now, "now")
    if not isinstance(policy, SourceQualityPolicy):
        raise TypeError("policy must be SourceQualityPolicy")

    reasons: list[str] = []
    if observation.schema_version != policy.schema_version:
        reasons.append("SCHEMA_VERSION_MISMATCH")
    if not observation.transport_verified:
        reasons.append("TRANSPORT_UNVERIFIED")
    if not observation.provenance_bound:
        reasons.append("PROVENANCE_UNBOUND")

    age = now - observation.observed_at
    if age < timedelta(0):
        reasons.append("FUTURE_OBSERVATION")
    elif age > policy.max_age:
        reasons.append("STALE_OBSERVATION")

    # Caller-supplied corroborator identities remain useful diagnostic metadata,
    # but this boundary has no product-owned corroborator authority resolver yet.
    # Therefore a positive corroboration truth value is structurally unavailable.
    corroborated = False
    input_confidence = observation.base_confidence

    if reasons:
        return SourceQualityAssessment(
            action=ConfidenceAction.ABSTAIN,
            input_confidence=input_confidence,
            reasons=tuple(reasons),
            corroborated=corroborated,
        )

    # SourceClass is caller-supplied.  There is no durable product-owned issuance
    # resolver in this authority family yet, so treating OFFICIAL_API as trusted
    # here would be an authority-forgery path.
    if observation.source_class is SourceClass.OFFICIAL_API:
        return SourceQualityAssessment(
            action=ConfidenceAction.ABSTAIN,
            input_confidence=input_confidence,
            reasons=("OFFICIAL_API_AUTHORITY_UNRESOLVED",),
            corroborated=corroborated,
        )

    if input_confidence < policy.downweight_confidence:
        return SourceQualityAssessment(
            action=ConfidenceAction.ABSTAIN,
            input_confidence=input_confidence,
            reasons=("CONFIDENCE_BELOW_DOWNWEIGHT_FLOOR",),
            corroborated=corroborated,
        )

    return SourceQualityAssessment(
        action=ConfidenceAction.DOWNWEIGHT,
        input_confidence=input_confidence,
        reasons=(f"{observation.source_class.value}_CANNOT_MINT_ACCEPT",),
        corroborated=corroborated,
    )


def validate_independent_corroborators(
    observation: SourceQualityObservation,
    allowed_corroborator_ids: Iterable[str],
) -> tuple[str, ...]:
    """Narrow caller-declared identities; do not mint corroboration authority.

    The returned intersection is diagnostic metadata only.  ``allowed`` is an
    input to this helper, not a product-owned trust token, and cannot make an
    assessment report ``corroborated=True``.  A future positive corroboration
    path must re-resolve identities from canonical durable authority.
    """

    if not isinstance(observation, SourceQualityObservation):
        raise TypeError("observation must be SourceQualityObservation")
    if isinstance(allowed_corroborator_ids, (str, bytes)):
        raise ValueError("allowed_corroborator_ids must be an iterable of identities, not text")
    try:
        allowed = {_strict_text(value, "allowed_corroborator_id") for value in allowed_corroborator_ids}
    except TypeError as exc:
        raise ValueError("allowed_corroborator_ids must be an iterable of identities") from exc
    return tuple(value for value in observation.corroborator_ids if value in allowed)


def _strict_text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{name} must be non-empty trimmed text")
    return value


def _validate_probability(value: object, name: str) -> None:
    if (
        type(value) is not Decimal
        or value.is_nan()
        or value.is_infinite()
        or not (Decimal(0) <= value <= Decimal(1))
    ):
        raise ValueError(f"{name} must be finite Decimal in [0, 1]")


def _validate_aware(value: object, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware datetime")
