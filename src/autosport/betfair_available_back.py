from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

_SUPPORTED_PRICE_LADDERS = {"CLASSIC", "FINEST"}
_CLASSIC_PRICE_BANDS: tuple[tuple[Decimal, Decimal, Decimal], ...] = (
    (Decimal("1.01"), Decimal("2.00"), Decimal("0.01")),
    (Decimal("2.00"), Decimal("3.00"), Decimal("0.02")),
    (Decimal("3.00"), Decimal("4.00"), Decimal("0.05")),
    (Decimal("4.00"), Decimal("6.00"), Decimal("0.10")),
    (Decimal("6.00"), Decimal("10.00"), Decimal("0.20")),
    (Decimal("10.00"), Decimal("20.00"), Decimal("0.50")),
    (Decimal("20.00"), Decimal("30.00"), Decimal("1.00")),
    (Decimal("30.00"), Decimal("50.00"), Decimal("2.00")),
    (Decimal("50.00"), Decimal("100.00"), Decimal("5.00")),
    (Decimal("100.00"), Decimal("1000.00"), Decimal("10.00")),
)


@dataclass(frozen=True, slots=True)
class AvailableBackQuote:
    """One reconstructed observed Betfair available-to-back quote.

    `cache_verified` means the local ladder state was initialized by a provider image and
    then updated only by compatible deltas. It is evidence that the quote was present in
    the historical order book, not evidence that a real order would have filled.
    """

    decimal_odds: Decimal
    available_size: Decimal
    provider_price_field: str
    ladder_kind: str
    price_ladder_type: str
    cache_verified: bool


@dataclass(frozen=True, slots=True)
class AvailableBackUpdate:
    touched: bool
    quote: AvailableBackQuote | None


class BetfairAvailableBackBook:
    """Stateful decoder for Stream API `atb` (PRO) and `batb` (ADVANCED) deltas.

    The two encodings are intentionally not mixed between provider images. `atb` is
    keyed by price; `batb` is keyed by display level. A market image must initialize
    the cache before a reconstructed quote is considered verified. Every ladder delta
    is also bound to the marketDefinition-declared Betfair price ladder; unsupported,
    absent, out-of-range, or off-tick prices fail closed.
    """

    def __init__(self) -> None:
        self._mode: str | None = None
        self._price_ladder_type: str | None = None
        self._initialized = False
        self._atb: dict[Decimal, Decimal] = {}
        self._batb: dict[int, tuple[Decimal, Decimal]] = {}

    @property
    def mode(self) -> str | None:
        return self._mode

    @property
    def price_ladder_type(self) -> str | None:
        return self._price_ladder_type

    def reset(self) -> None:
        self._mode = None
        self._price_ladder_type = None
        self._initialized = False
        self._atb.clear()
        self._batb.clear()

    def apply(
        self,
        runner_change: dict[str, Any],
        *,
        image: bool = False,
        price_ladder_type: str | None = None,
    ) -> AvailableBackUpdate:
        if image:
            self.reset()

        has_atb = "atb" in runner_change
        has_batb = "batb" in runner_change
        if has_atb and has_batb:
            raise ValueError("runner change contains both atb and batb ladder encodings")
        if not has_atb and not has_batb:
            current = self.current_quote()
            # The ladder cache remains provider state until a ladder delta/image changes it.
            # If the provider emits only a new LTP while that cache exists, surface the
            # persisted available quote at the new publish time. Otherwise replay would
            # incorrectly downgrade latest state from order-book evidence to observational
            # LTP merely because the unchanged ladder was omitted from this delta.
            return AvailableBackUpdate(
                current is not None and runner_change.get("ltp") is not None,
                current,
            )

        ladder_type = _normalize_price_ladder_type(price_ladder_type)
        if self._price_ladder_type is not None and self._price_ladder_type != ladder_type:
            raise ValueError("Betfair price ladder type changed without a new image")
        self._price_ladder_type = ladder_type

        mode = "atb" if has_atb else "batb"
        if self._mode is not None and self._mode != mode:
            raise ValueError("Betfair available-to-back ladder encoding changed without a new image")
        self._mode = mode
        if image:
            self._initialized = True

        raw = runner_change[mode]
        if not isinstance(raw, list):
            raise ValueError(f"{mode} must be a list")
        if mode == "atb":
            self._apply_atb(raw, ladder_type=ladder_type)
        else:
            self._apply_batb(raw, ladder_type=ladder_type)
        return AvailableBackUpdate(True, self.current_quote())

    def current_quote(self) -> AvailableBackQuote | None:
        ladder_type = self._price_ladder_type
        if ladder_type is None:
            return None
        if self._mode == "atb":
            if not self._atb:
                return None
            price = max(self._atb)
            return AvailableBackQuote(
                decimal_odds=price,
                available_size=self._atb[price],
                provider_price_field="rc[].atb",
                ladder_kind="full_price_ladder",
                price_ladder_type=ladder_type,
                cache_verified=self._initialized,
            )
        if self._mode == "batb":
            # ADVANCED data is level-keyed. Level 0 is the provider's best available
            # back. If level 0 is absent we fail closed instead of promoting a deeper
            # level to best price ourselves.
            best = self._batb.get(0)
            if best is None:
                return None
            price, size = best
            return AvailableBackQuote(
                decimal_odds=price,
                available_size=size,
                provider_price_field="rc[].batb",
                ladder_kind="best_three_level_ladder",
                price_ladder_type=ladder_type,
                cache_verified=self._initialized,
            )
        return None

    def _apply_atb(self, rows: list[Any], *, ladder_type: str) -> None:
        for index, row in enumerate(rows):
            if not isinstance(row, list) or len(row) != 2:
                raise ValueError(f"atb[{index}] must be [price,size]")
            price = _decimal_number(row[0], field=f"atb[{index}].price", positive=True)
            _validate_execution_price(price, ladder_type=ladder_type, field=f"atb[{index}].price")
            size = _decimal_number(row[1], field=f"atb[{index}].size", non_negative=True)
            if size == 0:
                self._atb.pop(price, None)
            else:
                self._atb[price] = size

    def _apply_batb(self, rows: list[Any], *, ladder_type: str) -> None:
        for index, row in enumerate(rows):
            if not isinstance(row, list) or len(row) != 3:
                raise ValueError(f"batb[{index}] must be [level,price,size]")
            level_raw = row[0]
            if isinstance(level_raw, bool) or not isinstance(level_raw, int) or level_raw < 0:
                raise ValueError(f"batb[{index}].level must be a non-negative integer")
            size = _decimal_number(row[2], field=f"batb[{index}].size", non_negative=True)
            if size == 0:
                # Betfair removal deltas may carry a zero/sentinel price. The level is
                # the identity for batb, so no price assertion is needed for removal.
                self._batb.pop(level_raw, None)
                continue
            price = _decimal_number(row[1], field=f"batb[{index}].price", positive=True)
            _validate_execution_price(price, ladder_type=ladder_type, field=f"batb[{index}].price")
            self._batb[level_raw] = (price, size)


def _normalize_price_ladder_type(value: str | None) -> str:
    ladder_type = str(value or "").strip().upper()
    if not ladder_type:
        raise ValueError("marketDefinition priceLadderDefinition.type is required for available-to-back prices")
    if ladder_type not in _SUPPORTED_PRICE_LADDERS:
        raise ValueError(f"unsupported Betfair price ladder type {ladder_type!r} for available-to-back prices")
    return ladder_type


def _validate_execution_price(price: Decimal, *, ladder_type: str, field: str) -> None:
    if ladder_type == "FINEST":
        if price < Decimal("1.01") or price > Decimal("1000.00"):
            raise ValueError(f"{field} is outside Betfair FINEST price range 1.01..1000")
        if (price - Decimal("1.01")) % Decimal("0.01") != 0:
            raise ValueError(f"{field} is off the Betfair FINEST 0.01 price ladder")
        return

    if ladder_type == "CLASSIC":
        if price < Decimal("1.01") or price > Decimal("1000.00"):
            raise ValueError(f"{field} is outside Betfair CLASSIC price range 1.01..1000")
        for band_index, (lower, upper, increment) in enumerate(_CLASSIC_PRICE_BANDS):
            in_band = lower <= price <= upper if band_index == 0 else lower < price <= upper
            if not in_band:
                continue
            if (price - lower) % increment != 0:
                raise ValueError(
                    f"{field}={price} is off the Betfair CLASSIC price ladder increment {increment}"
                )
            return
        raise ValueError(f"{field}={price} is not on the Betfair CLASSIC price ladder")

    # `_normalize_price_ladder_type` guarantees this branch is unreachable; keep it
    # fail-closed in case future supported types are added without a validator.
    raise ValueError(f"unsupported Betfair price ladder type {ladder_type!r}")


def _decimal_number(
    value: Any,
    *,
    field: str,
    positive: bool = False,
    non_negative: bool = False,
) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a finite number")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} must be a finite number") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field} must be a finite number")
    if positive and parsed <= 0:
        raise ValueError(f"{field} must be positive")
    if non_negative and parsed < 0:
        raise ValueError(f"{field} must be non-negative")
    return parsed
