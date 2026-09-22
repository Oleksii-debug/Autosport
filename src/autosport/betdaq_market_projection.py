from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Mapping

from .betdaq_readonly_market_wire import (
    BetdaqGetPricesWireResponse,
    BetdaqMarketPrices,
    BetdaqPriceLevel,
    BetdaqSelectionPrices,
)
from .domain import MarketType
from .providers import ProviderBatch, ProviderQuote


SOURCE_ID = "betdaq"
_SQLITE_SEQUENCE_MIN = -(1 << 63)
_SQLITE_SEQUENCE_MAX = (1 << 63) - 1


class BetdaqProjectionError(ValueError):
    """Validated BETDAQ wire evidence cannot be projected canonically."""


@dataclass(frozen=True, slots=True)
class BetdaqMarketContext:
    """Stable identity supplied by the independently validated discovery plane."""

    provider_event_id: str
    market_type: MarketType = MarketType.OTHER
    sport: str | None = None

    def __post_init__(self) -> None:
        if (
            type(self.provider_event_id) is not str
            or not self.provider_event_id
            or self.provider_event_id.strip() != self.provider_event_id
        ):
            raise ValueError("provider_event_id must be a non-empty trimmed string")
        if ":" in self.provider_event_id or "|" in self.provider_event_id:
            raise ValueError("provider_event_id contains a reserved identity delimiter")
        if not isinstance(self.market_type, MarketType):
            raise TypeError("market_type must be MarketType")
        if self.sport is not None:
            if (
                type(self.sport) is not str
                or not self.sport
                or self.sport.strip() != self.sport
            ):
                raise ValueError("sport must be a non-empty trimmed string or None")


def _timestamp_text(value: object, field: str) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise BetdaqProjectionError(f"{field} must be a non-empty trimmed string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BetdaqProjectionError(f"{field} must be valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BetdaqProjectionError(f"{field} must be timezone-aware ISO-8601")
    return value


def _validate_sequence(value: object) -> int:
    if type(value) is not int:
        raise BetdaqProjectionError("sequence must be a non-boolean int")
    if value < _SQLITE_SEQUENCE_MIN or value > _SQLITE_SEQUENCE_MAX:
        raise BetdaqProjectionError("sequence must fit signed 64-bit SQLite INTEGER")
    return value


def _validate_decimal(
    value: object,
    field: str,
    *,
    greater_than_one: bool = False,
    nonnegative: bool = False,
) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise BetdaqProjectionError(f"{field} must be a finite Decimal")
    if greater_than_one and value <= 1:
        raise BetdaqProjectionError(f"{field} must be greater than one")
    if nonnegative and value < 0:
        raise BetdaqProjectionError(f"{field} must be non-negative")
    return value


def _provider_source_ts(response: BetdaqGetPricesWireResponse) -> str | None:
    created = response.provider_created_at
    text = response.provider_created_at_text
    if created is None and text is None:
        return None
    if created is None or text is None:
        raise BetdaqProjectionError(
            "provider_created_at and provider_created_at_text must be present together"
        )
    if not isinstance(created, datetime):
        raise BetdaqProjectionError("provider_created_at must be datetime when present")
    if created.tzinfo is None or created.utcoffset() is None:
        raise BetdaqProjectionError("provider_created_at must be timezone-aware")
    source_ts = _timestamp_text(text, "provider_created_at_text")
    parsed = datetime.fromisoformat(source_ts.replace("Z", "+00:00"))
    if parsed != created:
        raise BetdaqProjectionError(
            "provider_created_at_text conflicts with parsed provider timestamp"
        )
    return source_ts


def _validated_depth(
    levels: tuple[BetdaqPriceLevel, ...],
    *,
    provider_side: str,
) -> tuple[BetdaqPriceLevel, ...]:
    if type(levels) is not tuple:
        raise BetdaqProjectionError("provider price depth must be a tuple")
    prices: list[Decimal] = []
    for level in levels:
        if not isinstance(level, BetdaqPriceLevel):
            raise BetdaqProjectionError("provider price depth contains invalid level")
        if level.provider_side != provider_side:
            raise BetdaqProjectionError(
                f"{provider_side} depth contains conflicting provider_side"
            )
        prices.append(
            _validate_decimal(
                level.price,
                f"{provider_side} price",
                greater_than_one=True,
            )
        )
        _validate_decimal(
            level.stake,
            f"{provider_side} stake",
            nonnegative=True,
        )
    if len(set(prices)) != len(prices):
        raise BetdaqProjectionError(f"{provider_side} depth contains duplicate price")
    expected = sorted(prices, reverse=provider_side == "FOR")
    if prices != expected:
        raise BetdaqProjectionError(
            f"{provider_side} depth violates provider competitiveness order"
        )
    if levels and levels[0].stake <= 0:
        raise BetdaqProjectionError(
            f"{provider_side} top-of-book cannot claim zero available liquidity"
        )
    return levels


def _depth_metadata(levels: tuple[BetdaqPriceLevel, ...]) -> list[dict[str, str]]:
    return [
        {
            "price": str(level.price),
            "stake": str(level.stake),
        }
        for level in levels
    ]


def _quote_metadata(
    *,
    market: BetdaqMarketPrices,
    selection: BetdaqSelectionPrices,
    provider_side: str,
    levels: tuple[BetdaqPriceLevel, ...],
) -> dict[str, object]:
    deduction = selection.deduction_factor
    if deduction is not None:
        _validate_decimal(
            deduction,
            "selection deduction_factor",
            nonnegative=True,
        )
    return {
        "betdaq_market_name": market.name,
        "betdaq_market_type_code": market.market_type_code,
        "betdaq_market_status_code": market.status_code,
        "betdaq_market_start_time": market.start_time_text,
        "betdaq_withdrawal_sequence_number": market.withdrawal_sequence_number,
        "betdaq_is_play_market": market.is_play_market,
        "betdaq_is_in_running_allowed": market.is_in_running_allowed,
        "betdaq_is_managed_when_in_running": market.is_managed_when_in_running,
        "betdaq_is_currently_in_running": market.is_currently_in_running,
        "betdaq_in_running_delay_seconds": market.in_running_delay_seconds,
        "betdaq_selection_name": selection.name,
        "betdaq_selection_status_code": selection.status_code,
        "betdaq_selection_reset_count": selection.reset_count,
        "betdaq_selection_deduction_factor": (
            None if deduction is None else str(deduction)
        ),
        "betdaq_provider_side": provider_side,
        "betdaq_available_depth": _depth_metadata(levels),
        "betdaq_depth_projection": "top-of-book-only",
        "betdaq_top_available_stake": str(levels[0].stake),
    }


def _validate_market_scalar_truth(
    market: BetdaqMarketPrices,
    selection: BetdaqSelectionPrices,
) -> None:
    for value, field in (
        (market.market_id, "market_id"),
        (market.market_type_code, "market_type_code"),
        (market.status_code, "market_status_code"),
        (market.withdrawal_sequence_number, "withdrawal_sequence_number"),
        (market.in_running_delay_seconds, "in_running_delay_seconds"),
        (selection.selection_id, "selection_id"),
        (selection.status_code, "selection_status_code"),
        (selection.reset_count, "selection_reset_count"),
    ):
        if type(value) is not int or value < 0:
            raise BetdaqProjectionError(f"{field} must be a non-negative int")
    for value, field in (
        (market.name, "market_name"),
        (market.start_time_text, "market_start_time"),
        (selection.name, "selection_name"),
    ):
        if type(value) is not str or not value or value.strip() != value:
            raise BetdaqProjectionError(f"{field} must be a non-empty trimmed string")
    _timestamp_text(market.start_time_text, "market_start_time")
    for value, field in (
        (market.is_play_market, "is_play_market"),
        (market.is_in_running_allowed, "is_in_running_allowed"),
        (market.is_managed_when_in_running, "is_managed_when_in_running"),
        (market.is_currently_in_running, "is_currently_in_running"),
    ):
        if type(value) is not bool:
            raise BetdaqProjectionError(f"{field} must be bool")


def _project_side(
    *,
    market: BetdaqMarketPrices,
    selection: BetdaqSelectionPrices,
    context: BetdaqMarketContext,
    observed_ts: str,
    source_ts: str | None,
    sequence: int,
    provider_side: str,
    levels: tuple[BetdaqPriceLevel, ...],
) -> ProviderQuote | None:
    levels = _validated_depth(levels, provider_side=provider_side)
    if not levels:
        return None
    top = levels[0]
    exchange_side = "back" if provider_side == "FOR" else "lay"
    metadata = _quote_metadata(
        market=market,
        selection=selection,
        provider_side=provider_side,
        levels=levels,
    )
    return ProviderQuote(
        provider_event_id=context.provider_event_id,
        provider_market_id=str(market.market_id),
        provider_selection_id=str(selection.selection_id),
        decimal_odds=top.price,
        observed_ts=observed_ts,
        sequence=sequence,
        market_type=context.market_type,
        status=f"betdaq-status:{market.status_code}",
        source_ts=source_ts,
        metadata=metadata,
        sport=context.sport,
        exchange_side=exchange_side,
    )


def project_betdaq_get_prices(
    *,
    response: BetdaqGetPricesWireResponse,
    market_context: Mapping[int, BetdaqMarketContext],
    observed_ts: str,
    sequence: int,
) -> ProviderBatch:
    """Project one fully validated GetPrices scope into canonical top-of-book quotes.

    This function is deliberately network-free. It does not establish provider
    entitlement, freshness, execution admissibility, settlement, or write authority.
    """

    if not isinstance(response, BetdaqGetPricesWireResponse):
        raise TypeError("response must be BetdaqGetPricesWireResponse")
    if not isinstance(market_context, Mapping):
        raise TypeError("market_context must be a mapping")
    observed_ts = _timestamp_text(observed_ts, "observed_ts")
    sequence = _validate_sequence(sequence)
    if type(response.return_code) is not int or response.return_code != 0:
        raise BetdaqProjectionError("non-success BETDAQ response cannot be projected")
    if response.unavailable_markets:
        raise BetdaqProjectionError(
            "partial/unavailable BETDAQ market scope cannot publish canonical batch"
        )

    markets = response.markets
    if type(markets) is not tuple:
        raise BetdaqProjectionError("response markets must be a tuple")
    if any(not isinstance(market, BetdaqMarketPrices) for market in markets):
        raise BetdaqProjectionError("response contains invalid market evidence")
    market_ids = [market.market_id for market in markets]
    if len(set(market_ids)) != len(market_ids):
        raise BetdaqProjectionError("response contains duplicate market_id")

    if any(type(key) is not int for key in market_context):
        raise BetdaqProjectionError("market_context keys must be non-boolean ints")
    if set(market_context) != set(market_ids):
        raise BetdaqProjectionError(
            "market_context must exactly cover the projected response markets"
        )
    for value in market_context.values():
        if not isinstance(value, BetdaqMarketContext):
            raise BetdaqProjectionError(
                "market_context values must be BetdaqMarketContext"
            )

    source_ts = _provider_source_ts(response)
    quotes: list[ProviderQuote] = []
    for market in markets:
        context = market_context[market.market_id]
        if type(market.selections) is not tuple:
            raise BetdaqProjectionError("market selections must be a tuple")
        seen_selection_ids: set[int] = set()
        for selection in market.selections:
            if not isinstance(selection, BetdaqSelectionPrices):
                raise BetdaqProjectionError(
                    "market contains invalid selection evidence"
                )
            _validate_market_scalar_truth(market, selection)
            if selection.selection_id in seen_selection_ids:
                raise BetdaqProjectionError(
                    "market contains duplicate selection_id"
                )
            seen_selection_ids.add(selection.selection_id)
            for provider_side, levels in (
                ("FOR", selection.for_side_prices),
                ("AGAINST", selection.against_side_prices),
            ):
                quote = _project_side(
                    market=market,
                    selection=selection,
                    context=context,
                    observed_ts=observed_ts,
                    source_ts=source_ts,
                    sequence=sequence,
                    provider_side=provider_side,
                    levels=levels,
                )
                if quote is not None:
                    quotes.append(quote)

    return ProviderBatch(
        source_id=SOURCE_ID,
        quotes=tuple(quotes),
        cursor=None,
    )


__all__ = [
    "BetdaqMarketContext",
    "BetdaqProjectionError",
    "SOURCE_ID",
    "project_betdaq_get_prices",
]
