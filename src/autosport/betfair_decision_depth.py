from __future__ import annotations

"""Strict Betfair returned exchange-ladder parsing with explicit authority boundaries.

The authority in this module is intentionally narrow. It parses one
caller-supplied Betfair-shaped MarketBook payload and records only the returned
exchange ladder for an exact market, selection and side. The parser does not
prove that the payload originated from Betfair or that the caller-supplied time
was the network observation instant. Returned liquidity is also racy: it can
disappear before an order reaches the exchange. Therefore this module never
proves provider acceptance, fill, matched size, accepted odds, execution
latency, or settlement.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
import hashlib
import json
from typing import Any, Mapping


_SCHEMA_VERSION = 1
_PROVIDER_ID = "betfair"


class BetfairDecisionDepthError(ValueError):
    """Raised when a MarketBook cannot issue strict returned-depth evidence."""


class BetfairOrderSide(StrEnum):
    BACK = "BACK"
    LAY = "LAY"


class BetfairDecisionDepthStatus(StrEnum):
    PARSED_RETURNED_EXCHANGE_LADDER = "PARSED_RETURNED_EXCHANGE_LADDER"
    PARSED_RETURNED_BEST_OFFERS = "PARSED_RETURNED_EXCHANGE_LADDER"
    # Compatibility alias: "displayed" here means only the returned API levels,
    # not website-equivalent/full-depth/virtualised liquidity.
    PARSED_DISPLAYED_DEPTH = "PARSED_RETURNED_EXCHANGE_LADDER"


def _canonical_text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise BetfairDecisionDepthError(f"{field} must be non-empty canonical text")
    return value


def _selection_id(value: object, field: str = "selection_id") -> int:
    if type(value) is not int or value <= 0:
        raise BetfairDecisionDepthError(f"{field} must be a positive integer")
    return value


def _side(value: object) -> BetfairOrderSide:
    if type(value) is BetfairOrderSide:
        return value
    if type(value) is not str:
        raise BetfairDecisionDepthError("side must be BACK or LAY")
    try:
        return BetfairOrderSide(value)
    except ValueError as exc:
        raise BetfairDecisionDepthError("side must be BACK or LAY") from exc


def _observed_at(value: object) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise BetfairDecisionDepthError("observed_at must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _positive_decimal(value: object, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise BetfairDecisionDepthError(f"{field} must be a finite positive number")
    try:
        parsed = value if type(value) is Decimal else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise BetfairDecisionDepthError(
            f"{field} must be a finite positive number"
        ) from exc
    if not parsed.is_finite() or parsed <= 0:
        raise BetfairDecisionDepthError(f"{field} must be a finite positive number")
    return parsed


def _canonical_decimal(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class BetfairDepthLevel:
    """One exact returned exchange price/size level."""

    price: Decimal
    size: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "price", _positive_decimal(self.price, "level.price"))
        object.__setattr__(self, "size", _positive_decimal(self.size, "level.size"))

    def to_dict(self) -> dict[str, str]:
        return {
            "price": _canonical_decimal(self.price),
            "size": _canonical_decimal(self.size),
        }


@dataclass(frozen=True, slots=True)
class BetfairDecisionDepthSnapshot:
    """Immutable returned-depth evidence for one exact decision-side ladder."""

    market_id: str
    selection_id: int
    side: BetfairOrderSide
    observed_at: datetime
    market_status: str
    runner_status: str
    inplay: bool | None
    bet_delay_seconds: int | None
    levels: tuple[BetfairDepthLevel, ...]
    market_data_delayed: bool | None = None
    status: BetfairDecisionDepthStatus = field(
        default=BetfairDecisionDepthStatus.PARSED_RETURNED_EXCHANGE_LADDER, init=False
    )
    provider_id: str = field(default=_PROVIDER_ID, init=False)
    provider_snapshot_origin_proven: bool = field(default=False, init=False)
    observation_time_proven: bool = field(default=False, init=False)
    request_projection_proven: bool = field(default=False, init=False)
    virtual_prices_included_proven: bool = field(default=False, init=False)
    full_ladder_proven: bool = field(default=False, init=False)
    provider_acceptance_proven: bool = field(default=False, init=False)
    fill_proven: bool = field(default=False, init=False)
    accepted_odds_proven: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        _canonical_text(self.market_id, "market_id")
        _selection_id(self.selection_id)
        if type(self.side) is not BetfairOrderSide:
            raise BetfairDecisionDepthError("side must be a BetfairOrderSide")
        object.__setattr__(self, "observed_at", _observed_at(self.observed_at))
        if self.market_status != "OPEN":
            raise BetfairDecisionDepthError("market_status must be OPEN")
        if self.runner_status != "ACTIVE":
            raise BetfairDecisionDepthError("runner_status must be ACTIVE")
        if self.market_data_delayed is not None and type(self.market_data_delayed) is not bool:
            raise BetfairDecisionDepthError("market_data_delayed must be bool or None")
        if self.inplay is not None and type(self.inplay) is not bool:
            raise BetfairDecisionDepthError("inplay must be bool or None")
        if self.bet_delay_seconds is not None and (
            type(self.bet_delay_seconds) is not int or self.bet_delay_seconds < 0
        ):
            raise BetfairDecisionDepthError(
                "bet_delay_seconds must be a non-negative integer or None"
            )
        if type(self.levels) is not tuple or any(
            type(level) is not BetfairDepthLevel for level in self.levels
        ):
            raise BetfairDecisionDepthError(
                "levels must be a tuple of BetfairDepthLevel"
            )
        prices = [level.price for level in self.levels]
        if len(prices) != len(set(prices)):
            raise BetfairDecisionDepthError("depth contains duplicate price levels")
        expected = sorted(prices, reverse=self.side is BetfairOrderSide.BACK)
        if prices != expected:
            raise BetfairDecisionDepthError(
                "depth levels must be ordered best-to-worst for the requested side"
            )
        if self.status is not BetfairDecisionDepthStatus.PARSED_RETURNED_EXCHANGE_LADDER:
            raise BetfairDecisionDepthError("unsupported decision-depth status")
        if self.provider_id != _PROVIDER_ID:
            raise BetfairDecisionDepthError("provider_id must be betfair")

    def returned_size_at_or_better(self, limit_price: Decimal) -> Decimal:
        """Return size in the returned ladder meeting the LIMIT threshold.

        For BACK, higher odds are better and prices >= ``limit_price`` qualify.
        For LAY, lower odds are better and prices <= ``limit_price`` qualify.
        This covers only returned levels; it is not a full-ladder proof, fill
        forecast, or reservation.
        """

        threshold = _positive_decimal(limit_price, "limit_price")
        if self.side is BetfairOrderSide.BACK:
            qualifying = (level for level in self.levels if level.price >= threshold)
        else:
            qualifying = (level for level in self.levels if level.price <= threshold)
        return sum((level.size for level in qualifying), Decimal("0"))

    def displayed_size_at_or_better(self, limit_price: Decimal) -> Decimal:
        """Compatibility alias for returned API levels only, not full market depth."""

        return self.returned_size_at_or_better(limit_price)

    def returned_capacity_covers(
        self, requested_size: Decimal, limit_price: Decimal
    ) -> bool:
        """Whether returned snapshot levels reach ``requested_size`` at the limit."""

        size = _positive_decimal(requested_size, "requested_size")
        return self.returned_size_at_or_better(limit_price) >= size

    def displayed_capacity_covers(
        self, requested_size: Decimal, limit_price: Decimal
    ) -> bool:
        """Compatibility alias; False does not prove the full ladder lacks depth."""

        return self.returned_capacity_covers(requested_size, limit_price)

    def worst_returned_price_for_size(
        self, requested_size: Decimal, limit_price: Decimal
    ) -> Decimal | None:
        """Return marginal returned price needed to reach requested snapshot size.

        ``None`` means the returned levels do not contain enough qualifying size; it
        does not prove the full exchange ladder lacks more depth. The result remains
        racy and must never be described as an accepted/matched provider price.
        """

        size = _positive_decimal(requested_size, "requested_size")
        threshold = _positive_decimal(limit_price, "limit_price")
        running = Decimal("0")
        for level in self.levels:
            qualifies = (
                level.price >= threshold
                if self.side is BetfairOrderSide.BACK
                else level.price <= threshold
            )
            if not qualifies:
                break
            running += level.size
            if running >= size:
                return level.price
        return None

    def worst_displayed_price_for_size(
        self, requested_size: Decimal, limit_price: Decimal
    ) -> Decimal | None:
        """Compatibility alias for marginal price within returned levels only."""

        return self.worst_returned_price_for_size(requested_size, limit_price)

    @property
    def evidence_id(self) -> str:
        return _digest(self.to_dict(include_evidence_id=False))

    def to_dict(self, *, include_evidence_id: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": "autosport.betfair_decision_depth_snapshot",
            "schema_version": _SCHEMA_VERSION,
            "provider_id": self.provider_id,
            "market_id": self.market_id,
            "selection_id": self.selection_id,
            "side": self.side.value,
            "observed_at": self.observed_at.isoformat().replace("+00:00", "Z"),
            "market_status": self.market_status,
            "runner_status": self.runner_status,
            "market_data_delayed": self.market_data_delayed,
            "inplay": self.inplay,
            "bet_delay_seconds": self.bet_delay_seconds,
            "levels": [level.to_dict() for level in self.levels],
            "status": self.status.value,
            "provider_snapshot_origin_proven": self.provider_snapshot_origin_proven,
            "observation_time_proven": self.observation_time_proven,
            "request_projection_proven": self.request_projection_proven,
            "virtual_prices_included_proven": self.virtual_prices_included_proven,
            "full_ladder_proven": self.full_ladder_proven,
            "provider_acceptance_proven": self.provider_acceptance_proven,
            "fill_proven": self.fill_proven,
            "accepted_odds_proven": self.accepted_odds_proven,
        }
        if include_evidence_id:
            payload["evidence_id"] = _digest(payload)
        return payload


def issue_betfair_decision_depth_snapshot(
    market_book: Mapping[str, object],
    *,
    market_id: str,
    selection_id: int,
    side: BetfairOrderSide | str,
    observed_at: datetime,
) -> BetfairDecisionDepthSnapshot:
    """Parse one caller-supplied MarketBook into non-authorizing depth evidence.

    This function intentionally does not prove that ``market_book`` came directly
    from Betfair, that ``observed_at`` is the provider/network observation time,
    which PriceProjection/virtualisation produced the response, or whether returned
    exchange levels are a best-offers slice or the full available ladder. A
    product-owned transport/clock boundary may later bind those facts.
    """

    if not isinstance(market_book, Mapping):
        raise BetfairDecisionDepthError("market_book must be a mapping")
    expected_market_id = _canonical_text(market_id, "market_id")
    expected_selection_id = _selection_id(selection_id)
    resolved_side = _side(side)
    resolved_observed_at = _observed_at(observed_at)

    payload_market_id = _canonical_text(market_book.get("marketId"), "marketBook.marketId")
    if payload_market_id != expected_market_id:
        raise BetfairDecisionDepthError("marketBook.marketId does not match requested market")

    market_status = _canonical_text(market_book.get("status"), "marketBook.status")
    if market_status != "OPEN":
        raise BetfairDecisionDepthError("marketBook.status must be OPEN")

    market_data_delayed = market_book.get("isMarketDataDelayed")
    if market_data_delayed is not None and type(market_data_delayed) is not bool:
        raise BetfairDecisionDepthError(
            "marketBook.isMarketDataDelayed must be bool when present"
        )

    inplay = market_book.get("inplay")
    if inplay is not None and type(inplay) is not bool:
        raise BetfairDecisionDepthError("marketBook.inplay must be bool when present")

    bet_delay = market_book.get("betDelay")
    if bet_delay is not None and (type(bet_delay) is not int or bet_delay < 0):
        raise BetfairDecisionDepthError(
            "marketBook.betDelay must be a non-negative integer when present"
        )

    runners = market_book.get("runners")
    if type(runners) is not list:
        raise BetfairDecisionDepthError("marketBook.runners must be a list")

    matches: list[Mapping[str, object]] = []
    for index, runner in enumerate(runners):
        if not isinstance(runner, Mapping):
            raise BetfairDecisionDepthError(
                f"marketBook.runners[{index}] must be a mapping"
            )
        runner_selection_id = _selection_id(
            runner.get("selectionId"), f"marketBook.runners[{index}].selectionId"
        )
        if runner_selection_id == expected_selection_id:
            matches.append(runner)
    if len(matches) != 1:
        raise BetfairDecisionDepthError(
            "requested selection must appear exactly once in marketBook.runners"
        )
    runner = matches[0]

    runner_status = _canonical_text(runner.get("status"), "runner.status")
    if runner_status != "ACTIVE":
        raise BetfairDecisionDepthError("runner.status must be ACTIVE")

    exchange = runner.get("ex")
    if not isinstance(exchange, Mapping):
        raise BetfairDecisionDepthError("runner.ex must be a mapping")
    ladder_field = (
        "availableToBack"
        if resolved_side is BetfairOrderSide.BACK
        else "availableToLay"
    )
    if ladder_field not in exchange:
        raise BetfairDecisionDepthError(f"runner.ex.{ladder_field} is required")
    raw_levels = exchange[ladder_field]
    if type(raw_levels) is not list:
        raise BetfairDecisionDepthError(f"runner.ex.{ladder_field} must be a list")

    levels: list[BetfairDepthLevel] = []
    seen_prices: set[Decimal] = set()
    for index, raw_level in enumerate(raw_levels):
        if not isinstance(raw_level, Mapping):
            raise BetfairDecisionDepthError(
                f"runner.ex.{ladder_field}[{index}] must be a mapping"
            )
        price = _positive_decimal(
            raw_level.get("price"), f"runner.ex.{ladder_field}[{index}].price"
        )
        size = _positive_decimal(
            raw_level.get("size"), f"runner.ex.{ladder_field}[{index}].size"
        )
        if price in seen_prices:
            raise BetfairDecisionDepthError("depth contains duplicate price levels")
        seen_prices.add(price)
        levels.append(BetfairDepthLevel(price=price, size=size))

    levels.sort(
        key=lambda level: level.price,
        reverse=resolved_side is BetfairOrderSide.BACK,
    )
    return BetfairDecisionDepthSnapshot(
        market_id=expected_market_id,
        selection_id=expected_selection_id,
        side=resolved_side,
        observed_at=resolved_observed_at,
        market_status=market_status,
        runner_status=runner_status,
        market_data_delayed=market_data_delayed,
        inplay=inplay,
        bet_delay_seconds=bet_delay,
        levels=tuple(levels),
    )
