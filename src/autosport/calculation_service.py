from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Sequence

from .calculation import CalculationEngine, CalculationResult
from .domain import MarketEvent, MarketType
from .forecasting import parse_iso_timestamp


_SERVICE_VERSION = "calculation-service-v1"


@dataclass(frozen=True, slots=True)
class MarketQuoteEvidence:
    event_id: str
    market_id: str
    selection_id: str
    decimal_odds: str
    observed_ts: str
    source_id: str
    sequence: int
    market_type: str
    source_ts: str | None
    ingest_ts: str
    quote_evidence_sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "market_id": self.market_id,
            "selection_id": self.selection_id,
            "decimal_odds": self.decimal_odds,
            "observed_ts": self.observed_ts,
            "source_id": self.source_id,
            "sequence": self.sequence,
            "market_type": self.market_type,
            "source_ts": self.source_ts,
            "ingest_ts": self.ingest_ts,
            "quote_evidence_sha256": self.quote_evidence_sha256,
        }


@dataclass(frozen=True, slots=True)
class CalculationEvidence:
    service_version: str
    result: CalculationResult
    quote_sources: tuple[MarketQuoteEvidence, ...]
    causal_cutoff_ts: str
    real_money_execution: bool
    evidence_sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "service_version": self.service_version,
            "result": self.result.as_dict(),
            "quote_sources": [source.as_dict() for source in self.quote_sources],
            "causal_cutoff_ts": self.causal_cutoff_ts,
            "real_money_execution": self.real_money_execution,
            "evidence_sha256": self.evidence_sha256,
        }


class CalculationService:
    """Application boundary for calculations bound to exact product quote evidence.

    The service never reads storage, looks up a replacement quote, or exposes
    MarketEvent metadata/outcome state to the calculation engine. Callers must
    provide the exact event(s) already selected by the live or replay surface and
    an explicit causal cutoff. All numeric work remains delegated to
    CalculationEngine.
    """

    def __init__(self, engine: CalculationEngine | None = None) -> None:
        self._engine = engine if engine is not None else CalculationEngine()

    def odds_conversion_for_event(
        self,
        event: MarketEvent,
        *,
        causal_cutoff_ts: str,
    ) -> CalculationEvidence:
        source, cutoff = _snapshot_quote(event, causal_cutoff_ts=causal_cutoff_ts)
        result = self._engine.odds_conversion(source.decimal_odds)
        return _bind(result, (source,), cutoff)

    def implied_probability_for_event(
        self,
        event: MarketEvent,
        *,
        causal_cutoff_ts: str,
    ) -> CalculationEvidence:
        source, cutoff = _snapshot_quote(event, causal_cutoff_ts=causal_cutoff_ts)
        result = self._engine.implied_probability(source.decimal_odds)
        return _bind(result, (source,), cutoff)

    def expected_return_for_event(
        self,
        event: MarketEvent,
        probability: Decimal | str | int,
        *,
        stake: Decimal | str | int = Decimal("1"),
        causal_cutoff_ts: str,
    ) -> CalculationEvidence:
        source, cutoff = _snapshot_quote(event, causal_cutoff_ts=causal_cutoff_ts)
        result = self._engine.expected_return(probability, source.decimal_odds, stake)
        return _bind(result, (source,), cutoff)

    def paper_payout_for_event(
        self,
        event: MarketEvent,
        stake: Decimal | str | int,
        *,
        causal_cutoff_ts: str,
    ) -> CalculationEvidence:
        source, cutoff = _snapshot_quote(event, causal_cutoff_ts=causal_cutoff_ts)
        result = self._engine.paper_payout(stake, source.decimal_odds)
        return _bind(result, (source,), cutoff)

    def fractional_kelly_for_event(
        self,
        event: MarketEvent,
        probability: Decimal | str | int,
        *,
        fraction: Decimal | str | int = Decimal("1"),
        cap: Decimal | str | int = Decimal("1"),
        causal_cutoff_ts: str,
    ) -> CalculationEvidence:
        source, cutoff = _snapshot_quote(event, causal_cutoff_ts=causal_cutoff_ts)
        result = self._engine.fractional_kelly(
            probability,
            source.decimal_odds,
            fraction=fraction,
            cap=cap,
        )
        return _bind(result, (source,), cutoff)

    def multiplicative_devig_for_market(
        self,
        events: Sequence[MarketEvent],
        *,
        causal_cutoff_ts: str,
    ) -> CalculationEvidence:
        if isinstance(events, (str, bytes)) or not isinstance(events, Sequence):
            raise ValueError("events must be an ordered sequence of MarketEvent values")
        if len(events) < 2:
            raise ValueError("market de-vig requires at least two selected quotes")

        cutoff_value, cutoff = _causal_cutoff(causal_cutoff_ts)
        snapshots = tuple(
            _snapshot_quote_at_cutoff(event, cutoff_value=cutoff_value)
            for event in events
        )
        first = snapshots[0]
        market_identity = (first.event_id, first.market_id, first.source_id, first.market_type)
        seen_selections: set[str] = set()
        for source in snapshots:
            if (source.event_id, source.market_id, source.source_id, source.market_type) != market_identity:
                raise ValueError(
                    "market de-vig quotes must belong to the same event, market, source, and market type"
                )
            if source.selection_id in seen_selections:
                raise ValueError("market de-vig requires one exact quote per selection")
            seen_selections.add(source.selection_id)

        ordered = tuple(sorted(snapshots, key=_quote_sort_key))
        result = self._engine.multiplicative_devig(
            {source.selection_id: source.decimal_odds for source in ordered}
        )
        return _bind(result, ordered, cutoff)


def _snapshot_quote(
    event: MarketEvent,
    *,
    causal_cutoff_ts: str,
) -> tuple[MarketQuoteEvidence, str]:
    cutoff_value, cutoff = _causal_cutoff(causal_cutoff_ts)
    return (
        _snapshot_quote_at_cutoff(event, cutoff_value=cutoff_value),
        cutoff,
    )


def _snapshot_quote_at_cutoff(
    event: MarketEvent,
    *,
    cutoff_value: datetime,
) -> MarketQuoteEvidence:
    if not isinstance(event, MarketEvent):
        raise ValueError("event must be a MarketEvent")
    if not isinstance(event.market_type, MarketType):
        raise ValueError("market event market_type must be a MarketType")

    # Intentionally construct the validation payload from quote-only scalar
    # fields. Do not call event.to_dict(): that would traverse mutable metadata
    # which may contain outcome/future-only material irrelevant to a calculation.
    raw = {
        "event_id": event.event_id,
        "market_id": event.market_id,
        "selection_id": event.selection_id,
        "decimal_odds": event.decimal_odds,
        "observed_ts": event.observed_ts,
        "source_id": event.source_id,
        "sequence": event.sequence,
        "market_type": event.market_type.value,
        "source_ts": event.source_ts,
        "ingest_ts": event.ingest_ts,
    }
    try:
        canonical = MarketEvent.from_dict(raw)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError("market event quote fields are not canonical") from exc

    observed = _timestamp(canonical.observed_ts, field="observed_ts")
    _timestamp(canonical.ingest_ts, field="ingest_ts")
    if canonical.source_ts is not None:
        _timestamp(canonical.source_ts, field="source_ts")
    if observed > cutoff_value:
        raise ValueError("selected quote observed_ts is after the calculation causal cutoff")

    payload = {
        "event_id": canonical.event_id,
        "market_id": canonical.market_id,
        "selection_id": canonical.selection_id,
        "decimal_odds": str(canonical.decimal_odds),
        "observed_ts": canonical.observed_ts,
        "source_id": canonical.source_id,
        "sequence": canonical.sequence,
        "market_type": canonical.market_type.value,
        "source_ts": canonical.source_ts,
        "ingest_ts": canonical.ingest_ts,
    }
    digest = _sha256(payload)
    return MarketQuoteEvidence(
        **payload,
        quote_evidence_sha256=digest,
    )


def _causal_cutoff(value: str) -> tuple[datetime, str]:
    parsed = _timestamp(value, field="causal_cutoff_ts")
    return parsed, parsed.astimezone(timezone.utc).isoformat()


def _timestamp(value: object, *, field: str) -> datetime:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{field} must be a non-empty trimmed timezone-aware ISO timestamp")
    try:
        return parse_iso_timestamp(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a timezone-aware ISO timestamp") from exc


def _bind(
    result: CalculationResult,
    sources: tuple[MarketQuoteEvidence, ...],
    cutoff: str,
) -> CalculationEvidence:
    ordered = tuple(sorted(sources, key=_quote_sort_key))
    payload = {
        "service_version": _SERVICE_VERSION,
        "result": result.as_dict(),
        "quote_sources": [source.as_dict() for source in ordered],
        "causal_cutoff_ts": cutoff,
        "real_money_execution": False,
    }
    return CalculationEvidence(
        service_version=_SERVICE_VERSION,
        result=result,
        quote_sources=ordered,
        causal_cutoff_ts=cutoff,
        real_money_execution=False,
        evidence_sha256=_sha256(payload),
    )


def _quote_sort_key(source: MarketQuoteEvidence) -> tuple[object, ...]:
    return (
        source.event_id,
        source.market_id,
        source.selection_id,
        source.source_id,
        source.sequence,
        source.observed_ts,
        source.quote_evidence_sha256,
    )


def _sha256(payload: object) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
