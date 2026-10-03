from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum


class LiveMarketAbstentionError(ValueError):
    """Raised when the abstention gate itself receives malformed authority input."""


class LiveMarketEligibility(str, Enum):
    WAIT = "WAIT"
    ELIGIBLE_FOR_DOWNSTREAM_EVALUATION = "ELIGIBLE_FOR_DOWNSTREAM_EVALUATION"


class MarketStatus(str, Enum):
    OPEN = "OPEN"
    SUSPENDED = "SUSPENDED"
    CLOSED = "CLOSED"
    INACTIVE = "INACTIVE"
    UNKNOWN = "UNKNOWN"


class ContinuityStatus(str, Enum):
    COHERENT = "COHERENT"
    GAP = "GAP"
    RECONCILING = "RECONCILING"
    UNKNOWN = "UNKNOWN"


class ProviderHealth(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNKNOWN = "UNKNOWN"


class AbstentionReason(str, Enum):
    FUTURE_QUOTE = "FUTURE_QUOTE"
    STALE_QUOTE = "STALE_QUOTE"
    MARKET_NOT_OPEN = "MARKET_NOT_OPEN"
    MARKET_DATA_DELAYED_OR_UNKNOWN = "MARKET_DATA_DELAYED_OR_UNKNOWN"
    CONTINUITY_NOT_COHERENT = "CONTINUITY_NOT_COHERENT"
    RESPONSE_COVERAGE_INCOMPLETE = "RESPONSE_COVERAGE_INCOMPLETE"
    CONTINUITY_EPOCH_MISMATCH = "CONTINUITY_EPOCH_MISMATCH"
    PROVIDER_HEALTH_NOT_HEALTHY = "PROVIDER_HEALTH_NOT_HEALTHY"
    SOURCE_ACTIONABILITY_UNPROVEN = "SOURCE_ACTIONABILITY_UNPROVEN"
    EXPLICIT_AMBIGUITY = "EXPLICIT_AMBIGUITY"
    INSUFFICIENT_INTERMEDIATE_GRANULARITY = "INSUFFICIENT_INTERMEDIATE_GRANULARITY"


def _require_aware(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise LiveMarketAbstentionError(f"{name} must be datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise LiveMarketAbstentionError(f"{name} must be timezone-aware")
    return value


def _require_bool(value: bool, name: str) -> bool:
    if type(value) is not bool:
        raise LiveMarketAbstentionError(f"{name} must be bool")
    return value


def _require_epoch(value: str, name: str) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise LiveMarketAbstentionError(f"{name} must be a non-empty canonical string")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise LiveMarketAbstentionError(f"{name} contains control characters")
    return value


@dataclass(frozen=True, slots=True)
class LiveMarketEligibilityInput:
    quote_observed_at: datetime
    decision_observed_at: datetime
    max_quote_age: timedelta
    market_status: MarketStatus
    market_data_delayed: bool | None
    continuity_status: ContinuityStatus
    response_coverage_complete: bool
    continuity_epoch: str
    expected_continuity_epoch: str
    provider_health: ProviderHealth
    source_actionability_proven: bool
    explicit_ambiguity: bool = False
    stream_conflated: bool = False
    requires_intermediate_granularity: bool = False

    def __post_init__(self) -> None:
        _require_aware(self.quote_observed_at, "quote_observed_at")
        _require_aware(self.decision_observed_at, "decision_observed_at")
        if not isinstance(self.max_quote_age, timedelta) or self.max_quote_age <= timedelta(0):
            raise LiveMarketAbstentionError("max_quote_age must be a positive timedelta")
        if not isinstance(self.market_status, MarketStatus):
            raise LiveMarketAbstentionError("market_status must be MarketStatus")
        if self.market_data_delayed is not None:
            _require_bool(self.market_data_delayed, "market_data_delayed")
        if not isinstance(self.continuity_status, ContinuityStatus):
            raise LiveMarketAbstentionError("continuity_status must be ContinuityStatus")
        _require_bool(self.response_coverage_complete, "response_coverage_complete")
        _require_epoch(self.continuity_epoch, "continuity_epoch")
        _require_epoch(self.expected_continuity_epoch, "expected_continuity_epoch")
        if not isinstance(self.provider_health, ProviderHealth):
            raise LiveMarketAbstentionError("provider_health must be ProviderHealth")
        _require_bool(self.source_actionability_proven, "source_actionability_proven")
        _require_bool(self.explicit_ambiguity, "explicit_ambiguity")
        _require_bool(self.stream_conflated, "stream_conflated")
        _require_bool(
            self.requires_intermediate_granularity,
            "requires_intermediate_granularity",
        )


@dataclass(frozen=True, slots=True)
class LiveMarketEligibilityDecision:
    status: LiveMarketEligibility
    reasons: tuple[AbstentionReason, ...]
    quote_age: timedelta

    def __post_init__(self) -> None:
        if type(self.status) is not LiveMarketEligibility:
            raise LiveMarketAbstentionError(
                "status must be exact LiveMarketEligibility"
            )
        if type(self.reasons) is not tuple or any(
            type(reason) is not AbstentionReason for reason in self.reasons
        ):
            raise LiveMarketAbstentionError(
                "reasons must be a tuple of exact AbstentionReason values"
            )
        if not isinstance(self.quote_age, timedelta):
            raise LiveMarketAbstentionError("quote_age must be timedelta")
        if self.status is LiveMarketEligibility.WAIT and not self.reasons:
            raise LiveMarketAbstentionError("WAIT decision requires abstention reasons")
        if (
            self.status is LiveMarketEligibility.ELIGIBLE_FOR_DOWNSTREAM_EVALUATION
            and self.reasons
        ):
            raise LiveMarketAbstentionError(
                "ELIGIBLE decision cannot contain abstention reasons"
            )

    @property
    def execution_authorized(self) -> bool:
        return False

    @property
    def provider_authorities_bound(self) -> bool:
        return False

    @property
    def eligible_for_downstream_evaluation(self) -> bool:
        return self.status is LiveMarketEligibility.ELIGIBLE_FOR_DOWNSTREAM_EVALUATION


def evaluate_live_market_eligibility(
    evidence: LiveMarketEligibilityInput,
) -> LiveMarketEligibilityDecision:
    """Fail closed before downstream economics when live market truth is not actionable.

    This is a stateless composition gate. It does not create provider freshness,
    continuity, market-status, coverage, or execution authority. It only consumes
    those independently established facts and decides whether downstream evaluation
    may inspect the snapshot. A positive result never authorizes execution.
    """

    if not isinstance(evidence, LiveMarketEligibilityInput):
        raise LiveMarketAbstentionError(
            "evidence must be canonical LiveMarketEligibilityInput"
        )

    age = evidence.decision_observed_at - evidence.quote_observed_at
    reasons: list[AbstentionReason] = []

    if age < timedelta(0):
        reasons.append(AbstentionReason.FUTURE_QUOTE)
    elif age >= evidence.max_quote_age:
        reasons.append(AbstentionReason.STALE_QUOTE)

    if evidence.market_status is not MarketStatus.OPEN:
        reasons.append(AbstentionReason.MARKET_NOT_OPEN)

    if evidence.market_data_delayed is not False:
        reasons.append(AbstentionReason.MARKET_DATA_DELAYED_OR_UNKNOWN)

    if evidence.continuity_status is not ContinuityStatus.COHERENT:
        reasons.append(AbstentionReason.CONTINUITY_NOT_COHERENT)

    if not evidence.response_coverage_complete:
        reasons.append(AbstentionReason.RESPONSE_COVERAGE_INCOMPLETE)

    if evidence.continuity_epoch != evidence.expected_continuity_epoch:
        reasons.append(AbstentionReason.CONTINUITY_EPOCH_MISMATCH)

    if evidence.provider_health is not ProviderHealth.HEALTHY:
        reasons.append(AbstentionReason.PROVIDER_HEALTH_NOT_HEALTHY)

    if not evidence.source_actionability_proven:
        reasons.append(AbstentionReason.SOURCE_ACTIONABILITY_UNPROVEN)

    if evidence.explicit_ambiguity:
        reasons.append(AbstentionReason.EXPLICIT_AMBIGUITY)

    if evidence.stream_conflated and evidence.requires_intermediate_granularity:
        reasons.append(AbstentionReason.INSUFFICIENT_INTERMEDIATE_GRANULARITY)

    if reasons:
        return LiveMarketEligibilityDecision(
            status=LiveMarketEligibility.WAIT,
            reasons=tuple(reasons),
            quote_age=age,
        )

    return LiveMarketEligibilityDecision(
        status=LiveMarketEligibility.ELIGIBLE_FOR_DOWNSTREAM_EVALUATION,
        reasons=(),
        quote_age=age,
    )
