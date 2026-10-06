"""Causal multi-provider quote comparison over one coherent market snapshot.

This module is a read-only intelligence projection. It never claims provider-universe
completeness, executable liquidity, accepted odds, arbitrage, or real-money edge.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from .domain import MarketEvent
from .market_mirror import MirrorSnapshot


class ProviderQuoteComparisonError(ValueError):
    """The supplied snapshot cannot support an unambiguous comparison."""


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value.strip() != value or "\x00" in value:
        raise ProviderQuoteComparisonError(
            f"{name} must be a non-empty canonical string"
        )
    value.encode("utf-8")
    return value


def _optional_text(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _text(value, name)


def _sha256(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ProviderQuoteComparisonError(
            f"{name} must be a canonical lowercase SHA-256 digest"
        )
    return text


def _exact_decimal_difference(left: Decimal, right: Decimal) -> Decimal:
    """Subtract finite Decimals without consulting the ambient Decimal context.

    ``Decimal`` arithmetic normally uses the process-local context precision, which is
    inappropriate for a value that participates in a deterministic evidence hash. Aligning
    the finite base-10 coefficients with integers preserves every input digit and constructs
    the exact Decimal result directly.
    """

    left_tuple = left.as_tuple()
    right_tuple = right.as_tuple()
    common_exponent = min(left_tuple.exponent, right_tuple.exponent)

    def signed_coefficient(value: Decimal) -> int:
        parts = value.as_tuple()
        coefficient = 0
        for digit in parts.digits:
            coefficient = coefficient * 10 + digit
        coefficient *= 10 ** (parts.exponent - common_exponent)
        return -coefficient if parts.sign else coefficient

    difference = signed_coefficient(left) - signed_coefficient(right)
    sign = 1 if difference < 0 else 0
    absolute = abs(difference)
    digits = tuple(int(character) for character in str(absolute)) if absolute else (0,)
    return Decimal((sign, digits, common_exponent))


def _instant(value: object, name: str) -> datetime:
    if type(value) is not str or not value or value.strip() != value:
        raise ProviderQuoteComparisonError(
            f"{name} must be a non-empty canonical timestamp"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProviderQuoteComparisonError(
            f"{name} must be an ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProviderQuoteComparisonError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _canonical_boundary(as_of: datetime, max_age: timedelta) -> tuple[datetime, int]:
    if not isinstance(as_of, datetime):
        raise TypeError("as_of must be a datetime")
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ProviderQuoteComparisonError("as_of must be timezone-aware")
    if not isinstance(max_age, timedelta):
        raise TypeError("max_age must be a timedelta")
    if max_age < timedelta(0):
        raise ProviderQuoteComparisonError("max_age must be non-negative")
    microseconds = (
        max_age.days * 86_400_000_000
        + max_age.seconds * 1_000_000
        + max_age.microseconds
    )
    return as_of.astimezone(timezone.utc), microseconds


def _canonical_event(event: object) -> MarketEvent:
    if not isinstance(event, MarketEvent):
        raise ProviderQuoteComparisonError(
            "mirror snapshot events must be MarketEvent values"
        )
    try:
        return MarketEvent.from_dict(event.to_dict())
    except (TypeError, ValueError) as exc:
        raise ProviderQuoteComparisonError(
            "mirror snapshot contains a non-canonical market event"
        ) from exc


def _event_payload(event: MarketEvent) -> dict[str, Any]:
    return event.to_dict()


def _event_sha256(event: MarketEvent) -> str:
    return _digest(_event_payload(event))


def _snapshot_sha256(revision: int, events: tuple[MarketEvent, ...]) -> str:
    ordered = sorted(
        (_event_payload(event) for event in events),
        key=lambda item: (
            item.get("sport") or "",
            item["source_id"],
            item["event_id"],
            item["market_id"],
            item["selection_id"],
            item["sequence"],
            _digest(item),
        ),
    )
    return _digest(
        {
            "schema": "autosport.provider_quote_comparison.snapshot",
            "schema_version": 1,
            "revision": revision,
            "events": ordered,
        }
    )


@dataclass(frozen=True, slots=True)
class ProviderQuotePoint:
    source_id: str
    provider_source_class: str | None
    sequence: int
    decimal_odds: Decimal
    observed_ts: str
    source_ts: str
    ingest_ts: str
    market_event_sha256: str

    def __post_init__(self) -> None:
        _text(self.source_id, "quote source_id")
        _optional_text(self.provider_source_class, "quote provider_source_class")
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 0:
            raise ProviderQuoteComparisonError(
                "quote sequence must be a non-negative integer"
            )
        if not isinstance(self.decimal_odds, Decimal) or not self.decimal_odds.is_finite():
            raise ProviderQuoteComparisonError(
                "quote decimal_odds must be an exact finite Decimal"
            )
        if self.decimal_odds <= Decimal("1"):
            raise ProviderQuoteComparisonError(
                "quote decimal_odds must be greater than 1"
            )
        observed = _instant(self.observed_ts, "quote observed_ts")
        source_time = _instant(self.source_ts, "quote source_ts")
        ingested = _instant(self.ingest_ts, "quote ingest_ts")
        if source_time > observed or observed > ingested:
            raise ProviderQuoteComparisonError(
                "quote timestamps must satisfy source_ts <= observed_ts <= ingest_ts"
            )
        _sha256(self.market_event_sha256, "quote market_event_sha256")

    def to_payload(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "provider_source_class": self.provider_source_class,
            "sequence": self.sequence,
            "decimal_odds": str(self.decimal_odds),
            "observed_ts": self.observed_ts,
            "source_ts": self.source_ts,
            "ingest_ts": self.ingest_ts,
            "market_event_sha256": self.market_event_sha256,
        }


@dataclass(frozen=True, slots=True)
class ProviderQuoteComparison:
    mirror_revision: int
    mirror_snapshot_sha256: str
    sport: str
    event_id: str
    market_id: str
    selection_id: str
    competition_id: str
    market_semantics_id: str
    as_of: str
    max_age_microseconds: int
    quotes: tuple[ProviderQuotePoint, ...]

    def __post_init__(self) -> None:
        if (
            isinstance(self.mirror_revision, bool)
            or not isinstance(self.mirror_revision, int)
            or self.mirror_revision < 0
        ):
            raise ProviderQuoteComparisonError(
                "mirror_revision must be a non-negative integer"
            )
        _sha256(self.mirror_snapshot_sha256, "mirror_snapshot_sha256")
        for name in (
            "sport",
            "event_id",
            "market_id",
            "selection_id",
            "competition_id",
            "market_semantics_id",
        ):
            _text(getattr(self, name), name)
        _instant(self.as_of, "as_of")
        if (
            isinstance(self.max_age_microseconds, bool)
            or not isinstance(self.max_age_microseconds, int)
            or self.max_age_microseconds < 0
        ):
            raise ProviderQuoteComparisonError(
                "max_age_microseconds must be a non-negative integer"
            )
        if type(self.quotes) is not tuple or len(self.quotes) < 2:
            raise ProviderQuoteComparisonError(
                "quotes must be a tuple containing at least two providers"
            )
        if any(type(quote) is not ProviderQuotePoint for quote in self.quotes):
            raise ProviderQuoteComparisonError(
                "quotes must contain exact ProviderQuotePoint values"
            )
        source_ids = tuple(quote.source_id for quote in self.quotes)
        if len(source_ids) != len(set(source_ids)):
            raise ProviderQuoteComparisonError(
                "comparison quotes must have unique provider source_ids"
            )
        if source_ids != tuple(sorted(source_ids)):
            raise ProviderQuoteComparisonError(
                "comparison quotes must be sorted by provider source_id"
            )

    @property
    def provider_count(self) -> int:
        return len(self.quotes)

    @property
    def best_decimal_odds(self) -> Decimal:
        return max(quote.decimal_odds for quote in self.quotes)

    @property
    def worst_decimal_odds(self) -> Decimal:
        return min(quote.decimal_odds for quote in self.quotes)

    @property
    def displayed_spread(self) -> Decimal:
        return _exact_decimal_difference(
            self.best_decimal_odds,
            self.worst_decimal_odds,
        )

    @property
    def best_source_ids(self) -> tuple[str, ...]:
        best = self.best_decimal_odds
        return tuple(
            quote.source_id
            for quote in self.quotes
            if quote.decimal_odds == best
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": "autosport.provider_quote_comparison",
            "schema_version": 1,
            "mirror_revision": self.mirror_revision,
            "mirror_snapshot_sha256": self.mirror_snapshot_sha256,
            "sport": self.sport,
            "event_id": self.event_id,
            "market_id": self.market_id,
            "selection_id": self.selection_id,
            "competition_id": self.competition_id,
            "market_semantics_id": self.market_semantics_id,
            "as_of": self.as_of,
            "max_age_microseconds": self.max_age_microseconds,
            "provider_count": self.provider_count,
            "best_decimal_odds": str(self.best_decimal_odds),
            "worst_decimal_odds": str(self.worst_decimal_odds),
            "displayed_spread": str(self.displayed_spread),
            "best_source_ids": list(self.best_source_ids),
            "quotes": [quote.to_payload() for quote in self.quotes],
            "claims": {
                "provider_universe_complete": False,
                "execution_feasible": False,
                "arbitrage_proven": False,
                "accepted_odds_proven": False,
                "real_money_edge_proven": False,
            },
        }

    @property
    def comparison_sha256(self) -> str:
        return _digest(self.to_payload())


def compare_provider_quotes(
    snapshot: MirrorSnapshot,
    *,
    as_of: datetime,
    max_age: timedelta,
    minimum_sources: int = 2,
) -> tuple[ProviderQuoteComparison, ...]:
    """Compare fresh exact-semantic quotes from distinct providers.

    The input may come from MarketMirror.view or MarketMirror.active_view.
    Freshness, future-leakage and status constraints are rechecked here so a
    caller-created MirrorSnapshot cannot bypass the comparison boundary.

    Only exact normalized sport/event/market/selection identities with explicit
    competition and market-semantics identities are comparable. Missing semantics
    or provider source time remain observable in the mirror but are excluded here
    instead of being guessed or upgraded from local receipt time.
    """

    if not isinstance(snapshot, MirrorSnapshot):
        raise TypeError("snapshot must be a MirrorSnapshot")
    if (
        isinstance(snapshot.revision, bool)
        or not isinstance(snapshot.revision, int)
        or snapshot.revision < 0
    ):
        raise ProviderQuoteComparisonError(
            "mirror snapshot revision must be a non-negative integer"
        )
    if (
        isinstance(minimum_sources, bool)
        or not isinstance(minimum_sources, int)
        or minimum_sources < 2
    ):
        raise ProviderQuoteComparisonError(
            "minimum_sources must be an integer greater than or equal to 2"
        )

    boundary, max_age_microseconds = _canonical_boundary(as_of, max_age)
    canonical_events = tuple(_canonical_event(event) for event in snapshot.events)
    snapshot_hash = _snapshot_sha256(snapshot.revision, canonical_events)

    groups: dict[
        tuple[str, str, str, str, str, str],
        dict[str, ProviderQuotePoint],
    ] = {}

    for event in canonical_events:
        if event.status != "open":
            continue
        if (
            event.sport is None
            or event.competition_id is None
            or event.market_semantics_id is None
        ):
            continue

        observed = _instant(event.observed_ts, "observed_ts")
        ingested = _instant(event.ingest_ts, "ingest_ts")
        if event.source_ts is None:
            # Receipt/observation recency is not provider quote freshness. Keep
            # receipt-only events observable in the mirror, but never promote
            # unknown upstream age into a positive fresh-provider comparison.
            continue
        source_time = _instant(event.source_ts, "source_ts")
        if source_time > observed or observed > ingested:
            raise ProviderQuoteComparisonError(
                "quote timestamps must satisfy source_ts <= observed_ts <= ingest_ts"
            )
        if observed > boundary or ingested > boundary or source_time > boundary:
            continue
        age = boundary - source_time
        age_microseconds = (
            age.days * 86_400_000_000
            + age.seconds * 1_000_000
            + age.microseconds
        )
        if age_microseconds > max_age_microseconds:
            continue

        key = (
            event.sport,
            event.event_id,
            event.market_id,
            event.selection_id,
            event.competition_id,
            event.market_semantics_id,
        )
        by_source = groups.setdefault(key, {})
        if event.source_id in by_source:
            raise ProviderQuoteComparisonError(
                "mirror snapshot contains duplicate source authority for one "
                "semantic quote identity"
            )
        by_source[event.source_id] = ProviderQuotePoint(
            source_id=event.source_id,
            provider_source_class=event.provider_source_class,
            sequence=event.sequence,
            decimal_odds=event.decimal_odds,
            observed_ts=event.observed_ts,
            source_ts=event.source_ts,
            ingest_ts=event.ingest_ts,
            market_event_sha256=_event_sha256(event),
        )

    result: list[ProviderQuoteComparison] = []
    for key in sorted(groups):
        by_source = groups[key]
        if len(by_source) < minimum_sources:
            continue
        sport, event_id, market_id, selection_id, competition_id, market_semantics_id = key
        quotes = tuple(by_source[source_id] for source_id in sorted(by_source))
        result.append(
            ProviderQuoteComparison(
                mirror_revision=snapshot.revision,
                mirror_snapshot_sha256=snapshot_hash,
                sport=sport,
                event_id=event_id,
                market_id=market_id,
                selection_id=selection_id,
                competition_id=competition_id,
                market_semantics_id=market_semantics_id,
                as_of=boundary.isoformat().replace("+00:00", "Z"),
                max_age_microseconds=max_age_microseconds,
                quotes=quotes,
            )
        )
    return tuple(result)
