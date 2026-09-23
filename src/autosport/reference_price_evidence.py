from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Iterable

from .domain import MarketEvent


class ReferencePriceEvidenceError(ValueError):
    """Raised when contemporaneous reference-price evidence is not truthful."""


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise ReferencePriceEvidenceError(f"{name} must be a non-empty trimmed string")
    return value


def _instant(value: object, name: str) -> datetime:
    raw = _text(value, name)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReferencePriceEvidenceError(
            f"{name} must be valid timezone-aware ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReferencePriceEvidenceError(
            f"{name} must be valid timezone-aware ISO-8601"
        )
    return parsed.astimezone(timezone.utc)


def _canonical_json(payload: object) -> str:
    try:
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ReferencePriceEvidenceError("payload must be canonical JSON data") from exc


def _canonical_json_sha256(payload: object) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _json_object_exact(raw: object, name: str) -> dict[str, object]:
    text = _text(raw, name)

    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in values:
            if key in result:
                raise ReferencePriceEvidenceError(f"{name} contains a duplicate key")
            result[key] = value
        return result

    try:
        payload = json.loads(
            text,
            object_pairs_hook=pairs,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ReferencePriceEvidenceError(
                    f"{name} contains non-finite JSON number {token}"
                )
            ),
        )
    except ReferencePriceEvidenceError:
        raise
    except (TypeError, ValueError) as exc:
        raise ReferencePriceEvidenceError(f"{name} must be valid JSON") from exc
    if type(payload) is not dict:
        raise ReferencePriceEvidenceError(f"{name} must encode a JSON object")
    if _canonical_json(payload) != text:
        raise ReferencePriceEvidenceError(f"{name} must use canonical JSON encoding")
    return payload


def _bounded_nonnegative_int(value: object, name: str, *, positive: bool = False) -> int:
    if type(value) is not int:
        raise ReferencePriceEvidenceError(f"{name} must be a non-boolean int")
    if value < 0 or (positive and value == 0):
        relation = "positive" if positive else "non-negative"
        raise ReferencePriceEvidenceError(f"{name} must be {relation}")
    return value


def _canonical_market_event(event: object) -> MarketEvent:
    if type(event) is not MarketEvent:
        raise ReferencePriceEvidenceError(
            "reference observations must be exact MarketEvent values"
        )
    try:
        return MarketEvent.from_dict(event.to_dict())
    except (TypeError, ValueError) as exc:
        raise ReferencePriceEvidenceError(
            "reference observation is not a canonical MarketEvent"
        ) from exc


def _market_event_from_canonical_json(raw: object) -> MarketEvent:
    text = _text(raw, "event_canonical_json")
    payload = _json_object_exact(text, "event_canonical_json")
    try:
        event = MarketEvent.from_dict(payload)
    except (TypeError, ValueError) as exc:
        raise ReferencePriceEvidenceError(
            "event_canonical_json does not encode a canonical MarketEvent"
        ) from exc
    if _canonical_json(event.to_dict()) != text:
        raise ReferencePriceEvidenceError(
            "event_canonical_json must be exact canonical MarketEvent bytes"
        )
    return event



class ReferenceTargetInclusionPolicy(str, Enum):
    """Whether the target provider may participate in the reference universe."""

    EXCLUDE = "EXCLUDE"
    INCLUDE = "INCLUDE"


@dataclass(frozen=True, slots=True)
class ReferencePriceProtocol:
    """Immutable predeclared policy for one contemporaneous reference calculation.

    The protocol freezes the complete eligible source universe and every policy
    knob that can change qualification or aggregation. Evidence built under one
    protocol therefore cannot silently omit an unfavorable eligible provider,
    self-include the target, widen freshness, or change aggregation semantics
    while retaining the same protocol identity.
    """

    eligible_source_ids: tuple[str, ...]
    target_price_source_id: str
    target_inclusion_policy: ReferenceTargetInclusionPolicy
    price_semantics: str
    max_age_seconds: int
    max_skew_seconds: int
    minimum_sources: int
    eligible_price_source_ids: tuple[str, ...] | None = None
    aggregation_method: str = "median_decimal_odds.v1"
    protocol_id: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.eligible_source_ids) is not tuple:
            raise ReferencePriceEvidenceError(
                "eligible_source_ids must be an exact tuple"
            )
        if not self.eligible_source_ids:
            raise ReferencePriceEvidenceError(
                "eligible_source_ids must not be empty"
            )
        source_ids = tuple(
            _text(source_id, "eligible source_id")
            for source_id in self.eligible_source_ids
        )
        if len(set(source_ids)) != len(source_ids):
            raise ReferencePriceEvidenceError(
                "eligible_source_ids must be unique"
            )
        source_ids = tuple(sorted(source_ids))
        object.__setattr__(self, "eligible_source_ids", source_ids)

        price_source_values = self.eligible_price_source_ids
        if price_source_values is None:
            price_source_values = source_ids
        if type(price_source_values) is not tuple or not price_source_values:
            raise ReferencePriceEvidenceError(
                "eligible_price_source_ids must be a non-empty exact tuple"
            )
        price_source_ids = tuple(
            _text(source_id, "eligible price source_id")
            for source_id in price_source_values
        )
        if len(set(price_source_ids)) != len(price_source_ids):
            raise ReferencePriceEvidenceError(
                "eligible_price_source_ids must be unique"
            )
        price_source_ids = tuple(sorted(price_source_ids))
        object.__setattr__(
            self,
            "eligible_price_source_ids",
            price_source_ids,
        )

        target = _text(self.target_price_source_id, "target_price_source_id")
        if type(self.target_inclusion_policy) is not ReferenceTargetInclusionPolicy:
            raise ReferencePriceEvidenceError(
                "target_inclusion_policy must be ReferenceTargetInclusionPolicy"
            )
        if (
            self.target_inclusion_policy
            is ReferenceTargetInclusionPolicy.EXCLUDE
            and target in price_source_ids
        ):
            raise ReferencePriceEvidenceError(
                "excluded target_price_source_id cannot appear in eligible_price_source_ids"
            )
        if (
            self.target_inclusion_policy
            is ReferenceTargetInclusionPolicy.INCLUDE
            and target not in price_source_ids
        ):
            raise ReferencePriceEvidenceError(
                "included target_price_source_id must appear in eligible_price_source_ids"
            )

        semantics = _text(self.price_semantics, "price_semantics")
        max_age = _bounded_nonnegative_int(
            self.max_age_seconds,
            "max_age_seconds",
            positive=True,
        )
        max_skew = _bounded_nonnegative_int(
            self.max_skew_seconds,
            "max_skew_seconds",
        )
        minimum = _bounded_nonnegative_int(
            self.minimum_sources,
            "minimum_sources",
            positive=True,
        )
        if minimum < 2:
            raise ReferencePriceEvidenceError(
                "minimum_sources must be at least 2"
            )
        if minimum > len(price_source_ids):
            raise ReferencePriceEvidenceError(
                "minimum_sources cannot exceed eligible independent price-source universe"
            )
        method = _text(self.aggregation_method, "aggregation_method")
        if method != "median_decimal_odds.v1":
            raise ReferencePriceEvidenceError(
                "unsupported reference aggregation_method"
            )

        object.__setattr__(self, "target_price_source_id", target)
        object.__setattr__(self, "price_semantics", semantics)
        object.__setattr__(self, "max_age_seconds", max_age)
        object.__setattr__(self, "max_skew_seconds", max_skew)
        object.__setattr__(self, "minimum_sources", minimum)
        object.__setattr__(self, "aggregation_method", method)
        object.__setattr__(
            self,
            "protocol_id",
            _canonical_json_sha256(self.identity_payload()),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema": "autosport.reference_price_protocol",
            "schema_version": 1,
            "eligible_source_ids": list(self.eligible_source_ids),
            "eligible_price_source_ids": list(self.eligible_price_source_ids),
            "target_price_source_id": self.target_price_source_id,
            "target_inclusion_policy": self.target_inclusion_policy.value,
            "price_semantics": self.price_semantics,
            "max_age_seconds": self.max_age_seconds,
            "max_skew_seconds": self.max_skew_seconds,
            "minimum_sources": self.minimum_sources,
            "aggregation_method": self.aggregation_method,
        }

    def to_dict(self) -> dict[str, object]:
        payload = self.identity_payload()
        payload["protocol_id"] = self.protocol_id
        return payload


@dataclass(frozen=True, slots=True)
class ReferenceObservation:
    """One exact canonical market observation used by a reference-price snapshot.

    Authority-bearing fields are derived from the exact canonical MarketEvent bytes.
    Callers cannot substitute a hash, price or clock independently of those bytes.
    """

    event_canonical_json: str

    def __post_init__(self) -> None:
        event = _market_event_from_canonical_json(self.event_canonical_json)
        if event.source_ts is None:
            raise ReferencePriceEvidenceError(
                "reference observation requires authoritative provider source_ts"
            )
        _text(
            event.metadata.get("bookmaker_key"),
            "reference observation metadata.bookmaker_key",
        )

    @classmethod
    def from_event(cls, event: MarketEvent) -> "ReferenceObservation":
        canonical = _canonical_market_event(event)
        if canonical.source_ts is None:
            raise ReferencePriceEvidenceError(
                "reference observation requires authoritative provider source_ts"
            )
        return cls(event_canonical_json=_canonical_json(canonical.to_dict()))

    def market_event(self) -> MarketEvent:
        return _market_event_from_canonical_json(self.event_canonical_json)

    @property
    def event_sha256(self) -> str:
        return hashlib.sha256(self.event_canonical_json.encode("utf-8")).hexdigest()

    @property
    def source_id(self) -> str:
        return self.market_event().source_id

    @property
    def price_source_id(self) -> str:
        return _text(
            self.market_event().metadata.get("bookmaker_key"),
            "reference observation metadata.bookmaker_key",
        )

    @property
    def sequence(self) -> int:
        return self.market_event().sequence

    @property
    def decimal_odds(self) -> Decimal:
        return self.market_event().decimal_odds

    @property
    def observed_ts(self) -> str:
        return self.market_event().observed_ts

    @property
    def source_ts(self) -> str:
        value = self.market_event().source_ts
        assert value is not None
        return value

    @property
    def ingest_ts(self) -> str:
        return self.market_event().ingest_ts

    @property
    def execution_quote_verified(self) -> bool:
        value = self.market_event().metadata.get("execution_quote_verified")
        if type(value) is not bool:
            raise ReferencePriceEvidenceError(
                "reference observation requires boolean execution_quote_verified"
            )
        return value

    def to_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "price_source_id": self.price_source_id,
            "event_sha256": self.event_sha256,
            "event_canonical_json": self.event_canonical_json,
            "sequence": self.sequence,
            "decimal_odds": str(self.decimal_odds),
            "observed_ts": self.observed_ts,
            "source_ts": self.source_ts,
            "ingest_ts": self.ingest_ts,
            "execution_quote_verified": self.execution_quote_verified,
        }


@dataclass(frozen=True, slots=True)
class ReferencePriceEvidence:
    """Deterministic self-validating multi-source observational evidence."""

    sport: str | None
    event_id: str
    market_id: str
    selection_id: str
    market_type: str
    market_semantics_id: str | None
    protocol: ReferencePriceProtocol
    decision_ts: str
    observations: tuple[ReferenceObservation, ...]
    evidence_id: str = field(init=False)
    median_decimal_odds: Decimal | None = field(init=False)
    lower_median_decimal_odds: Decimal = field(init=False)
    upper_median_decimal_odds: Decimal = field(init=False)
    min_decimal_odds: Decimal = field(init=False)
    max_decimal_odds: Decimal = field(init=False)
    executable_quote_verified: bool = field(default=False, init=False)
    fair_probability_verified: bool = field(default=False, init=False)
    fill_fidelity_verified: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        _validate_reference_price_evidence(self)

    @property
    def source_ids(self) -> tuple[str, ...]:
        return tuple(item.source_id for item in self.observations)

    @property
    def price_source_ids(self) -> tuple[str, ...]:
        return tuple(item.price_source_id for item in self.observations)

    @property
    def protocol_id(self) -> str:
        return self.protocol.protocol_id

    @property
    def price_semantics(self) -> str:
        return self.protocol.price_semantics

    @property
    def max_age_seconds(self) -> int:
        return self.protocol.max_age_seconds

    @property
    def max_skew_seconds(self) -> int:
        return self.protocol.max_skew_seconds

    @property
    def minimum_sources(self) -> int:
        return self.protocol.minimum_sources

    def to_dict(self) -> dict[str, object]:
        payload = _evidence_identity_payload(self)
        payload["evidence_id"] = self.evidence_id
        payload["source_ids"] = list(self.source_ids)
        return payload


def _median_band(values: tuple[Decimal, ...]) -> tuple[Decimal, Decimal]:
    ordered = tuple(sorted(values))
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint], ordered[midpoint]
    return ordered[midpoint - 1], ordered[midpoint]


def _evidence_identity_payload(evidence: ReferencePriceEvidence) -> dict[str, object]:
    return {
        "schema": "autosport.reference_price_evidence",
        "schema_version": 4,
        "protocol": evidence.protocol.to_dict(),
        "sport": evidence.sport,
        "event_id": evidence.event_id,
        "market_id": evidence.market_id,
        "selection_id": evidence.selection_id,
        "market_type": evidence.market_type,
        "market_semantics_id": evidence.market_semantics_id,
        "decision_ts": evidence.decision_ts,
        "observations": [item.to_dict() for item in evidence.observations],
        "median_decimal_odds": (
            None
            if evidence.median_decimal_odds is None
            else str(evidence.median_decimal_odds)
        ),
        "lower_median_decimal_odds": str(evidence.lower_median_decimal_odds),
        "upper_median_decimal_odds": str(evidence.upper_median_decimal_odds),
        "min_decimal_odds": str(evidence.min_decimal_odds),
        "max_decimal_odds": str(evidence.max_decimal_odds),
        "executable_quote_verified": False,
        "fair_probability_verified": False,
        "fill_fidelity_verified": False,
    }


def _validate_reference_price_evidence(evidence: ReferencePriceEvidence) -> None:
    if type(evidence.protocol) is not ReferencePriceProtocol:
        raise ReferencePriceEvidenceError(
            "protocol must be exact ReferencePriceProtocol"
        )
    if evidence.sport is not None:
        _text(evidence.sport, "sport")
    _text(evidence.event_id, "event_id")
    _text(evidence.market_id, "market_id")
    _text(evidence.selection_id, "selection_id")
    _text(evidence.market_type, "market_type")
    if evidence.market_semantics_id is not None:
        _text(evidence.market_semantics_id, "market_semantics_id")
    _text(evidence.price_semantics, "price_semantics")
    decision = _instant(evidence.decision_ts, "decision_ts")
    normalized_decision = decision.astimezone(timezone.utc).isoformat()
    object.__setattr__(evidence, "decision_ts", normalized_decision)
    max_age = _bounded_nonnegative_int(
        evidence.max_age_seconds, "max_age_seconds", positive=True
    )
    max_skew = _bounded_nonnegative_int(
        evidence.max_skew_seconds, "max_skew_seconds"
    )
    minimum = _bounded_nonnegative_int(
        evidence.minimum_sources, "minimum_sources", positive=True
    )
    if minimum < 2:
        raise ReferencePriceEvidenceError("minimum_sources must be at least 2")
    if type(evidence.observations) is not tuple:
        raise ReferencePriceEvidenceError("observations must be an exact tuple")
    if len(evidence.observations) < minimum:
        raise ReferencePriceEvidenceError(
            "reference-price evidence has insufficient source observations"
        )
    if any(type(item) is not ReferenceObservation for item in evidence.observations):
        raise ReferencePriceEvidenceError(
            "observations must contain exact ReferenceObservation values"
        )

    ordered = tuple(
        sorted(evidence.observations, key=lambda item: (item.source_id, item.event_sha256))
    )
    object.__setattr__(evidence, "observations", ordered)
    identity = (
        evidence.sport,
        evidence.event_id,
        evidence.market_id,
        evidence.selection_id,
        evidence.market_type,
        evidence.market_semantics_id,
    )
    source_ids: set[str] = set()
    price_source_ids: set[str] = set()
    source_times: list[datetime] = []
    ingest_times: list[datetime] = []
    odds: list[Decimal] = []

    for observation in ordered:
        event = observation.market_event()
        current_identity = (
            event.sport,
            event.event_id,
            event.market_id,
            event.selection_id,
            event.market_type.value,
            event.market_semantics_id,
        )
        if current_identity != identity:
            raise ReferencePriceEvidenceError(
                "reference observations must bind one exact market selection"
            )
        if event.status != "open":
            raise ReferencePriceEvidenceError(
                "reference observations require exact open market status"
            )
        source_ids.add(event.source_id)
        price_source_id = observation.price_source_id
        if price_source_id in price_source_ids:
            raise ReferencePriceEvidenceError(
                "reference observations require distinct independent price source_ids; "
                "the same bookmaker/source-of-price cannot gain multiple consensus votes"
            )
        price_source_ids.add(price_source_id)
        semantics = event.metadata.get("price_semantics")
        if type(semantics) is not str or not semantics or semantics.strip() != semantics:
            raise ReferencePriceEvidenceError(
                "reference observation requires explicit price_semantics"
            )
        if semantics != evidence.price_semantics:
            raise ReferencePriceEvidenceError(
                "reference observations cannot mix price semantics"
            )
        executable = event.metadata.get("execution_quote_verified")
        if type(executable) is not bool:
            raise ReferencePriceEvidenceError(
                "reference observation requires boolean execution_quote_verified"
            )

        observed = _instant(event.observed_ts, "event observed_ts")
        ingest = _instant(event.ingest_ts, "event ingest_ts")
        if event.source_ts is None:
            raise ReferencePriceEvidenceError(
                "reference observation requires authoritative provider source_ts"
            )
        source = _instant(event.source_ts, "event source_ts")
        if observed > ingest:
            raise ReferencePriceEvidenceError(
                "reference observation observed_ts cannot be after ingest_ts"
            )
        if source > ingest:
            raise ReferencePriceEvidenceError(
                "reference observation source_ts cannot be after ingest_ts"
            )
        for timestamp, name in (
            (observed, "observed_ts"),
            (ingest, "ingest_ts"),
            (source, "source_ts"),
        ):
            if timestamp > decision:
                raise ReferencePriceEvidenceError(
                    f"reference observation {name} is from the future"
                )
            if (decision - timestamp).total_seconds() > max_age:
                raise ReferencePriceEvidenceError(
                    f"reference observation {name} is stale"
                )
        source_times.append(source)
        ingest_times.append(ingest)
        odds.append(event.decimal_odds)

    expected_source_ids = set(evidence.protocol.eligible_source_ids)
    if source_ids != expected_source_ids:
        missing = tuple(sorted(expected_source_ids - source_ids))
        unexpected = tuple(sorted(source_ids - expected_source_ids))
        raise ReferencePriceEvidenceError(
            "reference observations must exactly cover frozen eligible transport-source "
            f"universe; missing={missing!r}; unexpected={unexpected!r}"
        )
    expected_price_source_ids = set(evidence.protocol.eligible_price_source_ids)
    if price_source_ids != expected_price_source_ids:
        missing = tuple(sorted(expected_price_source_ids - price_source_ids))
        unexpected = tuple(sorted(price_source_ids - expected_price_source_ids))
        raise ReferencePriceEvidenceError(
            "reference observations must exactly cover frozen eligible independent "
            f"price-source universe; missing={missing!r}; unexpected={unexpected!r}"
        )
    if len(price_source_ids) < minimum:
        raise ReferencePriceEvidenceError(
            "reference-price evidence has insufficient distinct independent price sources"
        )
    if (max(source_times) - min(source_times)).total_seconds() > max_skew:
        raise ReferencePriceEvidenceError(
            "reference source observations exceed maximum contemporaneous skew"
        )
    if (max(ingest_times) - min(ingest_times)).total_seconds() > max_skew:
        raise ReferencePriceEvidenceError(
            "reference receipt observations exceed maximum contemporaneous skew"
        )

    values = tuple(odds)
    lower_median, upper_median = _median_band(values)
    object.__setattr__(evidence, "lower_median_decimal_odds", lower_median)
    object.__setattr__(evidence, "upper_median_decimal_odds", upper_median)
    object.__setattr__(
        evidence,
        "median_decimal_odds",
        lower_median if lower_median == upper_median else None,
    )
    object.__setattr__(evidence, "min_decimal_odds", min(values))
    object.__setattr__(evidence, "max_decimal_odds", max(values))
    object.__setattr__(
        evidence,
        "evidence_id",
        _canonical_json_sha256(_evidence_identity_payload(evidence)),
    )


def build_reference_price_evidence(
    events: Iterable[MarketEvent],
    *,
    decision_ts: str,
    protocol: ReferencePriceProtocol | None = None,
    max_age_seconds: int | None = None,
    max_skew_seconds: int | None = None,
    minimum_sources: int | None = None,
) -> ReferencePriceEvidence:
    """Build exact contemporaneous evidence under one frozen reference protocol.

    Policy authority is never accepted as a collection of free per-call knobs:
    callers must supply one immutable protocol whose identity freezes the exact
    eligible provider universe, target inclusion rule, freshness/skew bounds,
    minimum coverage, price semantics and aggregation method.

    The protocol is a structural/frozen-policy contract, not provider-origin
    authority. Canonical provider/store provenance remains a separate boundary.
    """

    if protocol is None:
        raise ReferencePriceEvidenceError(
            "frozen ReferencePriceProtocol is required"
        )
    if type(protocol) is not ReferencePriceProtocol:
        raise ReferencePriceEvidenceError(
            "protocol must be exact ReferencePriceProtocol"
        )
    if any(
        value is not None
        for value in (
            max_age_seconds,
            max_skew_seconds,
            minimum_sources,
        )
    ):
        raise ReferencePriceEvidenceError(
            "per-call reference policy overrides are forbidden; "
            "use the frozen protocol"
        )

    decision = _instant(decision_ts, "decision_ts")
    materialized = tuple(events)
    if len(materialized) < protocol.minimum_sources:
        raise ReferencePriceEvidenceError(
            "reference-price evidence has insufficient source observations"
        )
    canonical = tuple(_canonical_market_event(event) for event in materialized)
    if not canonical:
        raise ReferencePriceEvidenceError(
            "reference-price evidence has no source observations"
        )

    first = canonical[0]
    semantics = first.metadata.get("price_semantics")
    if (
        type(semantics) is not str
        or not semantics
        or semantics.strip() != semantics
    ):
        raise ReferencePriceEvidenceError(
            "reference observation requires explicit price_semantics"
        )
    if semantics != protocol.price_semantics:
        raise ReferencePriceEvidenceError(
            "reference observation price_semantics does not match frozen protocol"
        )

    observations = tuple(
        ReferenceObservation.from_event(event)
        for event in canonical
    )
    return ReferencePriceEvidence(
        sport=first.sport,
        event_id=first.event_id,
        market_id=first.market_id,
        selection_id=first.selection_id,
        market_type=first.market_type.value,
        market_semantics_id=first.market_semantics_id,
        protocol=protocol,
        decision_ts=decision.astimezone(timezone.utc).isoformat(),
        observations=observations,
    )
