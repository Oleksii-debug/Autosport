from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum


class QuoteFreshnessTimestampKind(str, Enum):
    PROVIDER_SOURCE = "provider_source"
    LOCAL_OBSERVED = "local_observed"


class QuoteFreshnessVerdict(str, Enum):
    FRESH_PROVIDER = "fresh_provider"
    FRESH_RECEIPT_ONLY = "fresh_receipt_only"
    STALE = "stale"
    FUTURE = "future"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class QuoteFreshnessIdentity:
    source_id: str
    provider_id: str
    source_mode: str
    contract_version: str
    sport: str
    event_id: str
    market_id: str
    selection_id: str
    side: str


@dataclass(frozen=True, slots=True)
class QuoteFreshnessEvidence:
    identity: QuoteFreshnessIdentity
    quote_id: str
    provider_sequence: int | None
    source_ts: str | None
    observed_ts: str | None
    ingest_ts: str | None


@dataclass(frozen=True, slots=True)
class QuoteFreshnessAuthority:
    authority_id: str
    identity: QuoteFreshnessIdentity
    timestamp_kind: QuoteFreshnessTimestampKind
    max_age: timedelta
    max_future_skew: timedelta = timedelta(0)
    inclusive_max_age: bool = True
    require_monotonic_provider_sequence: bool = False


@dataclass(frozen=True, slots=True)
class QuoteFreshnessDecision:
    verdict: QuoteFreshnessVerdict
    reason: str
    age: timedelta | None = None
    timestamp_kind: QuoteFreshnessTimestampKind | None = None

    @property
    def decision_eligible(self) -> bool:
        return self.verdict in {
            QuoteFreshnessVerdict.FRESH_PROVIDER,
            QuoteFreshnessVerdict.FRESH_RECEIPT_ONLY,
        }

    @property
    def provider_freshness_proven(self) -> bool:
        return self.verdict is QuoteFreshnessVerdict.FRESH_PROVIDER


_IDENTITY_FIELDS = (
    "source_id",
    "provider_id",
    "source_mode",
    "contract_version",
    "sport",
    "event_id",
    "market_id",
    "selection_id",
    "side",
)


def _unknown(reason: str) -> QuoteFreshnessDecision:
    return QuoteFreshnessDecision(QuoteFreshnessVerdict.UNKNOWN, reason)


def _future(
    reason: str,
    *,
    timestamp_kind: QuoteFreshnessTimestampKind | None = None,
) -> QuoteFreshnessDecision:
    return QuoteFreshnessDecision(
        QuoteFreshnessVerdict.FUTURE,
        reason,
        timestamp_kind=timestamp_kind,
    )


def _canonical_text(value: object, *, field: str) -> str | None:
    if type(value) is not str or not value or value.strip() != value:
        return None
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return None
    return value


def _valid_identity(identity: object) -> bool:
    if not isinstance(identity, QuoteFreshnessIdentity):
        return False
    for field in _IDENTITY_FIELDS:
        if _canonical_text(getattr(identity, field), field=field) is None:
            return False
    return identity.side in {"BACK", "LAY"}


def _utc_timestamp(value: object) -> datetime | None:
    if type(value) is not str or not value or value.strip() != value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def _utc_decision_time(value: object) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    try:
        if value.tzinfo is None or value.utcoffset() is None:
            return None
        return value.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def _valid_nonnegative_timedelta(value: object) -> bool:
    return isinstance(value, timedelta) and value >= timedelta(0)


def _valid_policy(authority: object) -> bool:
    if not isinstance(authority, QuoteFreshnessAuthority):
        return False
    if _canonical_text(authority.authority_id, field="authority_id") is None:
        return False
    if not _valid_identity(authority.identity):
        return False
    if not isinstance(authority.timestamp_kind, QuoteFreshnessTimestampKind):
        return False
    if not _valid_nonnegative_timedelta(authority.max_age):
        return False
    if not _valid_nonnegative_timedelta(authority.max_future_skew):
        return False
    if type(authority.inclusive_max_age) is not bool:
        return False
    return type(authority.require_monotonic_provider_sequence) is bool


def _valid_sequence(value: object) -> bool:
    return type(value) is int and value >= 0


def evaluate_quote_freshness(
    evidence: QuoteFreshnessEvidence,
    *,
    decision_at: datetime,
    authority: QuoteFreshnessAuthority,
    previous_provider_sequence: int | None = None,
) -> QuoteFreshnessDecision:
    """Evaluate whether one exact quote is fresh enough for a decision.

    This boundary intentionally distinguishes provider-time freshness from mere local
    receipt recency. Passing this check proves only freshness under the supplied exact
    authority. It does not prove executable price, fill, settlement, profitability,
    real-money authorization, provider legality, or whole-product readiness.

    Runtime evidence fails closed: malformed identity, policy, clocks or sequence
    evidence returns ``UNKNOWN`` instead of raising and accidentally widening use.
    """

    if not isinstance(evidence, QuoteFreshnessEvidence):
        return _unknown("evidence is not QuoteFreshnessEvidence")
    if not _valid_policy(authority):
        return _unknown("freshness authority is malformed")
    if not _valid_identity(evidence.identity):
        return _unknown("quote freshness identity is malformed")
    if evidence.identity != authority.identity:
        return _unknown("quote identity does not match freshness authority")
    if _canonical_text(evidence.quote_id, field="quote_id") is None:
        return _unknown("quote_id is malformed")

    cutoff = _utc_decision_time(decision_at)
    if cutoff is None:
        return _unknown("decision_at must be a timezone-aware datetime")

    # Local receipt clocks are mandatory even when provider source time is the
    # freshness clock. Without them, the system cannot prove that the evidence was
    # causally available before the decision.
    observed = _utc_timestamp(evidence.observed_ts)
    ingested = _utc_timestamp(evidence.ingest_ts)
    if observed is None or ingested is None:
        return _unknown("observed_ts and ingest_ts are required canonical timestamps")
    if observed > ingested:
        return _unknown("observed_ts cannot be later than ingest_ts")
    if observed > cutoff or ingested > cutoff:
        return _future("quote was not causally available at decision time")

    source = None
    if evidence.source_ts is not None:
        source = _utc_timestamp(evidence.source_ts)
        if source is None:
            return _unknown("source_ts is malformed")

    if authority.require_monotonic_provider_sequence:
        if not _valid_sequence(evidence.provider_sequence):
            return _unknown("monotonic freshness authority requires provider_sequence")
        if previous_provider_sequence is not None:
            if not _valid_sequence(previous_provider_sequence):
                return _unknown("previous_provider_sequence is malformed")
            if evidence.provider_sequence <= previous_provider_sequence:
                return _unknown("provider_sequence did not advance monotonically")

    if authority.timestamp_kind is QuoteFreshnessTimestampKind.PROVIDER_SOURCE:
        if source is None:
            return _unknown("provider-source freshness requires source_ts")
        chosen = source
        fresh_verdict = QuoteFreshnessVerdict.FRESH_PROVIDER
    elif authority.timestamp_kind is QuoteFreshnessTimestampKind.LOCAL_OBSERVED:
        chosen = observed
        fresh_verdict = QuoteFreshnessVerdict.FRESH_RECEIPT_ONLY
    else:  # Defensive even though policy validation rejects this case.
        return _unknown("freshness timestamp kind is unsupported")

    if chosen > cutoff:
        ahead = chosen - cutoff
        if ahead > authority.max_future_skew:
            return _future(
                "freshness timestamp exceeds allowed future clock skew",
                timestamp_kind=authority.timestamp_kind,
            )
        age = timedelta(0)
    else:
        age = cutoff - chosen

    within_age = (
        age <= authority.max_age
        if authority.inclusive_max_age
        else age < authority.max_age
    )
    if not within_age:
        return QuoteFreshnessDecision(
            QuoteFreshnessVerdict.STALE,
            "quote exceeds freshness max_age",
            age=age,
            timestamp_kind=authority.timestamp_kind,
        )

    reason = (
        "provider source timestamp is fresh under exact authority"
        if fresh_verdict is QuoteFreshnessVerdict.FRESH_PROVIDER
        else "local receipt timestamp is fresh under explicit receipt-time authority"
    )
    return QuoteFreshnessDecision(
        fresh_verdict,
        reason,
        age=age,
        timestamp_kind=authority.timestamp_kind,
    )
