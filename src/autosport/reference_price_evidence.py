from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .domain import MarketEvent
from .market_mirror import MirrorSnapshot


_SCHEMA_VERSION = 1
_METHOD = "median-band-observed-decimal-odds-v1"



def _canonical_text(value: object, field_name: str) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise ValueError(f"{field_name} must be a non-empty trimmed string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field_name} must be UTF-8 encodable") from exc
    return value


def _validate_event_scalars(event: MarketEvent) -> None:
    _canonical_text(event.source_id, "source_id")
    _canonical_text(event.event_id, "event_id")
    _canonical_text(event.market_id, "market_id")
    _canonical_text(event.selection_id, "selection_id")
    if isinstance(event.sequence, bool) or not isinstance(event.sequence, int):
        raise ValueError("market event sequence must be a non-boolean integer")
    if not isinstance(event.decimal_odds, Decimal):
        raise ValueError("market event decimal_odds must be a Decimal")
    if not event.decimal_odds.is_finite() or event.decimal_odds <= 1:
        raise ValueError("market event decimal_odds must be finite and greater than 1")


def _utc_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _utc_iso8601(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _event_timestamp(event: MarketEvent) -> datetime:
    raw = event.source_ts or event.observed_ts
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError("market event freshness timestamp must be valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("market event freshness timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _event_local_timestamp(raw: str, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _duration_microseconds(value: object, field_name: str) -> int:
    if not isinstance(value, timedelta):
        raise TypeError(f"{field_name} must be a timedelta")
    if value < timedelta(0):
        raise ValueError(f"{field_name} must be non-negative")
    return (
        value.days * 86_400_000_000
        + value.seconds * 1_000_000
        + value.microseconds
    )


def _required_source_ids(values: object) -> frozenset[str]:
    if values is None:
        return frozenset()
    if isinstance(values, (str, bytes)):
        raise TypeError("required_source_ids must be an iterable of source identifiers")
    try:
        materialized = tuple(values)
    except TypeError as exc:
        raise TypeError(
            "required_source_ids must be an iterable of source identifiers"
        ) from exc
    if any(
        type(value) is not str or not value or value.strip() != value
        for value in materialized
    ):
        raise ValueError(
            "required_source_ids entries must be non-empty trimmed strings"
        )
    if len(set(materialized)) != len(materialized):
        raise ValueError("required_source_ids must not contain duplicates")
    return frozenset(materialized)


@dataclass(frozen=True, slots=True)
class ReferencePriceEvidence:
    """Read-only cross-provider market-reference evidence.

    This contract describes contemporaneous observed decimal odds only. It is not a
    fair-probability estimate, execution quote, provider certification, or permission
    to place a wager.
    """

    schema_version: int
    method: str
    mirror_revision: int
    quote_key: str
    sport: str
    competition_id: str | None
    event_id: str
    market_id: str
    selection_id: str
    market_semantics_id: str
    as_of: str
    max_age_microseconds: int
    max_source_spread_microseconds: int
    source_ids: tuple[str, ...]
    source_sequences: tuple[tuple[str, int], ...]
    source_effective_timestamps: tuple[tuple[str, str], ...]
    source_observed_timestamps: tuple[tuple[str, str], ...]
    source_ingest_timestamps: tuple[tuple[str, str], ...]
    source_decimal_odds: tuple[tuple[str, Decimal], ...]
    minimum_decimal_odds: Decimal
    lower_median_decimal_odds: Decimal
    upper_median_decimal_odds: Decimal
    maximum_decimal_odds: Decimal
    action_authorized: bool = field(init=False, default=False)
    fair_probability_proven: bool = field(init=False, default=False)
    execution_price_proven: bool = field(init=False, default=False)

    def _identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "method": self.method,
            "mirror_revision": self.mirror_revision,
            "quote_key": self.quote_key,
            "sport": self.sport,
            "competition_id": self.competition_id,
            "event_id": self.event_id,
            "market_id": self.market_id,
            "selection_id": self.selection_id,
            "market_semantics_id": self.market_semantics_id,
            "as_of": self.as_of,
            "max_age_microseconds": self.max_age_microseconds,
            "max_source_spread_microseconds": self.max_source_spread_microseconds,
            "source_ids": list(self.source_ids),
            "source_sequences": [list(value) for value in self.source_sequences],
            "source_effective_timestamps": [
                list(value) for value in self.source_effective_timestamps
            ],
            "source_observed_timestamps": [
                list(value) for value in self.source_observed_timestamps
            ],
            "source_ingest_timestamps": [
                list(value) for value in self.source_ingest_timestamps
            ],
            "source_decimal_odds": [
                [source_id, str(odds)] for source_id, odds in self.source_decimal_odds
            ],
            "minimum_decimal_odds": str(self.minimum_decimal_odds),
            "lower_median_decimal_odds": str(self.lower_median_decimal_odds),
            "upper_median_decimal_odds": str(self.upper_median_decimal_odds),
            "maximum_decimal_odds": str(self.maximum_decimal_odds),
            "action_authorized": self.action_authorized,
            "fair_probability_proven": self.fair_probability_proven,
            "execution_price_proven": self.execution_price_proven,
        }

    @property
    def evidence_sha256(self) -> str:
        encoded = json.dumps(
            self._identity_payload(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def build_reference_price_evidence(
    snapshot: MirrorSnapshot,
    *,
    as_of: datetime,
    max_age: timedelta,
    max_source_spread: timedelta,
    min_sources: int = 2,
    required_source_ids: object = None,
) -> ReferencePriceEvidence:
    """Build deterministic contemporaneous reference evidence from one mirror revision.

    Every event must describe the exact same canonical quote identity and market
    semantics. Freshness is revalidated using the same provider-time-preferred clock
    as ``MarketMirror.active_view``. Local observation/ingest clocks must also be
    causally available by ``as_of`` so a hand-built snapshot cannot inject future
    evidence. Distinct provider sources are required.

    The returned median band is descriptive raw decimal-odds evidence only. For an
    even provider count the lower/upper medians remain an interval rather than
    inventing a synthetic midpoint.
    """

    if not isinstance(snapshot, MirrorSnapshot):
        raise TypeError("snapshot must be a MirrorSnapshot")
    if isinstance(snapshot.revision, bool) or not isinstance(snapshot.revision, int):
        raise ValueError("snapshot revision must be a non-boolean integer")
    if snapshot.revision < 0:
        raise ValueError("snapshot revision must be non-negative")
    if type(snapshot.events) is not tuple:
        raise ValueError("snapshot events must be a tuple")
    if isinstance(min_sources, bool) or not isinstance(min_sources, int):
        raise ValueError("min_sources must be a non-boolean integer")
    if min_sources < 2 or min_sources > 100:
        raise ValueError("min_sources must be between 2 and 100")

    boundary = _utc_datetime(as_of, "as_of")
    max_age_us = _duration_microseconds(max_age, "max_age")
    max_spread_us = _duration_microseconds(
        max_source_spread,
        "max_source_spread",
    )
    required_sources = _required_source_ids(required_source_ids)

    events = snapshot.events
    if len(events) < min_sources:
        raise ValueError("reference evidence requires at least min_sources events")
    if len(events) > 100:
        raise ValueError("reference evidence supports at most 100 provider events")
    if any(not isinstance(event, MarketEvent) for event in events):
        raise TypeError("snapshot events must contain only MarketEvent values")

    first = events[0]
    if first.sport is None:
        raise ValueError("reference evidence requires explicit canonical sport identity")
    if first.market_semantics_id is None:
        raise ValueError("reference evidence requires canonical market_semantics_id")

    canonical_identity = (
        first.quote_key,
        first.sport,
        first.competition_id,
        first.event_id,
        first.market_id,
        first.selection_id,
        first.market_type,
        first.market_semantics_id,
    )

    by_source: dict[str, tuple[MarketEvent, datetime, datetime, datetime]] = {}
    for event in events:
        _validate_event_scalars(event)
        if event.status != "open":
            raise ValueError("reference evidence accepts only open market events")
        identity = (
            event.quote_key,
            event.sport,
            event.competition_id,
            event.event_id,
            event.market_id,
            event.selection_id,
            event.market_type,
            event.market_semantics_id,
        )
        if identity != canonical_identity:
            raise ValueError(
                "reference evidence cannot combine different quote or market semantic identities"
            )
        if event.source_id in by_source:
            raise ValueError("reference evidence requires distinct provider source_ids")

        effective = _event_timestamp(event)
        observed = _event_local_timestamp(event.observed_ts, "observed_ts")
        ingested = _event_local_timestamp(event.ingest_ts, "ingest_ts")
        if observed > boundary or ingested > boundary or effective > boundary:
            raise ValueError("reference evidence cannot contain future market evidence")
        if boundary - effective > max_age:
            raise ValueError("reference evidence contains stale market evidence")

        by_source[event.source_id] = (event, effective, observed, ingested)

    source_ids = tuple(sorted(by_source))
    if not required_sources.issubset(by_source):
        raise ValueError("reference evidence is missing a required provider source")

    effective_values = [by_source[source_id][1] for source_id in source_ids]
    if max(effective_values) - min(effective_values) > max_source_spread:
        raise ValueError(
            "reference evidence provider timestamps exceed max_source_spread"
        )

    sorted_odds = sorted(by_source[source_id][0].decimal_odds for source_id in source_ids)
    lower_index = (len(sorted_odds) - 1) // 2
    upper_index = len(sorted_odds) // 2

    return ReferencePriceEvidence(
        schema_version=_SCHEMA_VERSION,
        method=_METHOD,
        mirror_revision=snapshot.revision,
        quote_key=first.quote_key,
        sport=first.sport,
        competition_id=first.competition_id,
        event_id=first.event_id,
        market_id=first.market_id,
        selection_id=first.selection_id,
        market_semantics_id=first.market_semantics_id,
        as_of=_utc_iso8601(boundary),
        max_age_microseconds=max_age_us,
        max_source_spread_microseconds=max_spread_us,
        source_ids=source_ids,
        source_sequences=tuple(
            (source_id, by_source[source_id][0].sequence) for source_id in source_ids
        ),
        source_effective_timestamps=tuple(
            (source_id, _utc_iso8601(by_source[source_id][1]))
            for source_id in source_ids
        ),
        source_observed_timestamps=tuple(
            (source_id, _utc_iso8601(by_source[source_id][2]))
            for source_id in source_ids
        ),
        source_ingest_timestamps=tuple(
            (source_id, _utc_iso8601(by_source[source_id][3]))
            for source_id in source_ids
        ),
        source_decimal_odds=tuple(
            (source_id, by_source[source_id][0].decimal_odds)
            for source_id in source_ids
        ),
        minimum_decimal_odds=sorted_odds[0],
        lower_median_decimal_odds=sorted_odds[lower_index],
        upper_median_decimal_odds=sorted_odds[upper_index],
        maximum_decimal_odds=sorted_odds[-1],
    )
