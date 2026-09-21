from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
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


def _canonical_json_sha256(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _bounded_nonnegative_int(value: object, name: str, *, positive: bool = False) -> int:
    if type(value) is not int:
        raise ReferencePriceEvidenceError(f"{name} must be a non-boolean int")
    if value < 0 or (positive and value == 0):
        relation = "positive" if positive else "non-negative"
        raise ReferencePriceEvidenceError(f"{name} must be {relation}")
    return value


@dataclass(frozen=True, slots=True)
class ReferenceObservation:
    """One exact canonical market observation used by a reference-price snapshot."""

    source_id: str
    event_sha256: str
    sequence: int
    decimal_odds: Decimal
    observed_ts: str
    source_ts: str | None
    ingest_ts: str
    execution_quote_verified: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "event_sha256": self.event_sha256,
            "sequence": self.sequence,
            "decimal_odds": str(self.decimal_odds),
            "observed_ts": self.observed_ts,
            "source_ts": self.source_ts,
            "ingest_ts": self.ingest_ts,
            "execution_quote_verified": self.execution_quote_verified,
        }


@dataclass(frozen=True, slots=True)
class ReferencePriceEvidence:
    """Deterministic multi-source observational reference-price evidence.

    This is deliberately weaker than executable-price or fair-probability evidence.
    It records a contemporaneous cross-source median of like-for-like observed decimal
    odds and binds the exact canonical MarketEvent bytes that contributed to it.
    """

    evidence_id: str
    sport: str | None
    event_id: str
    market_id: str
    selection_id: str
    market_type: str
    market_semantics_id: str | None
    price_semantics: str
    decision_ts: str
    max_age_seconds: int
    max_skew_seconds: int
    minimum_sources: int
    observations: tuple[ReferenceObservation, ...]
    median_decimal_odds: Decimal
    min_decimal_odds: Decimal
    max_decimal_odds: Decimal
    executable_quote_verified: bool = False
    fair_probability_verified: bool = False
    fill_fidelity_verified: bool = False

    @property
    def source_ids(self) -> tuple[str, ...]:
        return tuple(item.source_id for item in self.observations)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "autosport.reference_price_evidence",
            "schema_version": 1,
            "evidence_id": self.evidence_id,
            "sport": self.sport,
            "event_id": self.event_id,
            "market_id": self.market_id,
            "selection_id": self.selection_id,
            "market_type": self.market_type,
            "market_semantics_id": self.market_semantics_id,
            "price_semantics": self.price_semantics,
            "decision_ts": self.decision_ts,
            "max_age_seconds": self.max_age_seconds,
            "max_skew_seconds": self.max_skew_seconds,
            "minimum_sources": self.minimum_sources,
            "source_ids": list(self.source_ids),
            "observations": [item.to_dict() for item in self.observations],
            "median_decimal_odds": str(self.median_decimal_odds),
            "min_decimal_odds": str(self.min_decimal_odds),
            "max_decimal_odds": str(self.max_decimal_odds),
            "executable_quote_verified": self.executable_quote_verified,
            "fair_probability_verified": self.fair_probability_verified,
            "fill_fidelity_verified": self.fill_fidelity_verified,
        }


def _median(values: tuple[Decimal, ...]) -> Decimal:
    ordered = tuple(sorted(values))
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / Decimal(2)


def _event_hash(event: MarketEvent) -> str:
    return _canonical_json_sha256(event.to_dict())


def build_reference_price_evidence(
    events: Iterable[MarketEvent],
    *,
    decision_ts: str,
    max_age_seconds: int,
    max_skew_seconds: int,
    minimum_sources: int = 2,
) -> ReferencePriceEvidence:
    """Build exact contemporaneous reference-odds evidence from canonical events.

    Required observations are same-selection, same market semantics, OPEN, received
    no later than decision time, fresh on source/observation/ingest clocks, and drawn
    from distinct source_id values. The resulting median is an observational statistic
    only; no executable-price, fill-fidelity, or fair-probability claim is granted.
    """

    max_age = _bounded_nonnegative_int(
        max_age_seconds, "max_age_seconds", positive=True
    )
    max_skew = _bounded_nonnegative_int(max_skew_seconds, "max_skew_seconds")
    minimum = _bounded_nonnegative_int(
        minimum_sources, "minimum_sources", positive=True
    )
    if minimum < 2:
        raise ReferencePriceEvidenceError("minimum_sources must be at least 2")

    decision = _instant(decision_ts, "decision_ts")
    materialized = tuple(events)
    if len(materialized) < minimum:
        raise ReferencePriceEvidenceError(
            "reference-price evidence has insufficient source observations"
        )

    canonical_events: list[MarketEvent] = []
    event_payloads: list[dict[str, object]] = []
    for event in materialized:
        if type(event) is not MarketEvent:
            raise ReferencePriceEvidenceError(
                "reference observations must be exact MarketEvent values"
            )
        try:
            payload = event.to_dict()
            canonical = MarketEvent.from_dict(payload)
        except (TypeError, ValueError) as exc:
            raise ReferencePriceEvidenceError(
                "reference observation is not a canonical MarketEvent"
            ) from exc
        canonical_events.append(canonical)
        event_payloads.append(payload)

    first = canonical_events[0]
    identity = (
        first.sport,
        first.event_id,
        first.market_id,
        first.selection_id,
        first.market_type.value,
        first.market_semantics_id,
    )
    source_ids: set[str] = set()
    price_semantics: str | None = None
    observations: list[ReferenceObservation] = []
    quote_times: list[datetime] = []
    ingest_times: list[datetime] = []

    for event, payload in zip(canonical_events, event_payloads, strict=True):
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
        if event.source_id in source_ids:
            raise ReferencePriceEvidenceError(
                "reference observations require distinct source_id values"
            )
        source_ids.add(event.source_id)

        metadata = event.metadata
        semantics = metadata.get("price_semantics")
        executable = metadata.get("execution_quote_verified")
        if type(semantics) is not str or not semantics or semantics.strip() != semantics:
            raise ReferencePriceEvidenceError(
                "reference observation requires explicit price_semantics"
            )
        if not isinstance(executable, bool):
            raise ReferencePriceEvidenceError(
                "reference observation requires boolean execution_quote_verified"
            )
        if price_semantics is None:
            price_semantics = semantics
        elif semantics != price_semantics:
            raise ReferencePriceEvidenceError(
                "reference observations cannot mix price semantics"
            )

        observed = _instant(event.observed_ts, "event observed_ts")
        ingest = _instant(event.ingest_ts, "event ingest_ts")
        source = (
            None if event.source_ts is None else _instant(event.source_ts, "event source_ts")
        )
        if observed > ingest:
            raise ReferencePriceEvidenceError(
                "reference observation observed_ts cannot be after ingest_ts"
            )
        if source is not None and source > ingest:
            raise ReferencePriceEvidenceError(
                "reference observation source_ts cannot be after ingest_ts"
            )
        for timestamp, name in (
            (observed, "observed_ts"),
            (ingest, "ingest_ts"),
            *((() if source is None else ((source, "source_ts"),))),
        ):
            if timestamp > decision:
                raise ReferencePriceEvidenceError(
                    f"reference observation {name} is from the future"
                )
            if (decision - timestamp).total_seconds() > max_age:
                raise ReferencePriceEvidenceError(
                    f"reference observation {name} is stale"
                )

        quote_time = source or observed
        quote_times.append(quote_time)
        ingest_times.append(ingest)
        observations.append(
            ReferenceObservation(
                source_id=event.source_id,
                event_sha256=_canonical_json_sha256(payload),
                sequence=event.sequence,
                decimal_odds=event.decimal_odds,
                observed_ts=event.observed_ts,
                source_ts=event.source_ts,
                ingest_ts=event.ingest_ts,
                execution_quote_verified=executable,
            )
        )

    if len(source_ids) < minimum:
        raise ReferencePriceEvidenceError(
            "reference-price evidence has insufficient distinct sources"
        )
    if (
        max(quote_times) - min(quote_times)
    ).total_seconds() > max_skew:
        raise ReferencePriceEvidenceError(
            "reference source observations exceed maximum contemporaneous skew"
        )
    if (
        max(ingest_times) - min(ingest_times)
    ).total_seconds() > max_skew:
        raise ReferencePriceEvidenceError(
            "reference receipt observations exceed maximum contemporaneous skew"
        )

    observations.sort(key=lambda item: (item.source_id, item.event_sha256))
    ordered = tuple(observations)
    odds = tuple(item.decimal_odds for item in ordered)
    median = _median(odds)
    minimum_odds = min(odds)
    maximum_odds = max(odds)
    assert price_semantics is not None

    identity_payload = {
        "schema": "autosport.reference_price_evidence",
        "schema_version": 1,
        "sport": first.sport,
        "event_id": first.event_id,
        "market_id": first.market_id,
        "selection_id": first.selection_id,
        "market_type": first.market_type.value,
        "market_semantics_id": first.market_semantics_id,
        "price_semantics": price_semantics,
        "decision_ts": decision.astimezone(timezone.utc).isoformat(),
        "max_age_seconds": max_age,
        "max_skew_seconds": max_skew,
        "minimum_sources": minimum,
        "observations": [item.to_dict() for item in ordered],
        "median_decimal_odds": str(median),
        "min_decimal_odds": str(minimum_odds),
        "max_decimal_odds": str(maximum_odds),
        "executable_quote_verified": False,
        "fair_probability_verified": False,
        "fill_fidelity_verified": False,
    }
    return ReferencePriceEvidence(
        evidence_id=_canonical_json_sha256(identity_payload),
        sport=first.sport,
        event_id=first.event_id,
        market_id=first.market_id,
        selection_id=first.selection_id,
        market_type=first.market_type.value,
        market_semantics_id=first.market_semantics_id,
        price_semantics=price_semantics,
        decision_ts=identity_payload["decision_ts"],
        max_age_seconds=max_age,
        max_skew_seconds=max_skew,
        minimum_sources=minimum,
        observations=ordered,
        median_decimal_odds=median,
        min_decimal_odds=minimum_odds,
        max_decimal_odds=maximum_odds,
    )
