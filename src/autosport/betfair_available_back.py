from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


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
    cache_verified: bool


@dataclass(frozen=True, slots=True)
class AvailableBackUpdate:
    touched: bool
    quote: AvailableBackQuote | None


class BetfairAvailableBackBook:
    """Stateful decoder for Stream API `atb` (PRO) and `batb` (ADVANCED) deltas.

    The two encodings are intentionally not mixed between provider images. `atb` is
    keyed by price; `batb` is keyed by display level. A market image must initialize
    the cache before a reconstructed quote is considered verified.
    """

    def __init__(self) -> None:
        self._mode: str | None = None
        self._initialized = False
        self._atb: dict[Decimal, Decimal] = {}
        self._batb: dict[int, tuple[Decimal, Decimal]] = {}

    @property
    def mode(self) -> str | None:
        return self._mode

    def reset(self) -> None:
        self._mode = None
        self._initialized = False
        self._atb.clear()
        self._batb.clear()

    def apply(self, runner_change: dict[str, Any], *, image: bool = False) -> AvailableBackUpdate:
        has_atb = "atb" in runner_change
        has_batb = "batb" in runner_change
        if has_atb and has_batb:
            raise ValueError("runner change contains both atb and batb ladder encodings")
        if not has_atb and not has_batb:
            if image:
                self.reset()
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

        mode = "atb" if has_atb else "batb"
        if not image and self._mode is not None and self._mode != mode:
            raise ValueError("Betfair available-to-back ladder encoding changed without a new image")

        raw = runner_change[mode]
        if not isinstance(raw, list):
            raise ValueError(f"{mode} must be a list")

        # Apply each provider message transactionally. A late malformed ladder row must
        # not leave earlier rows (or an image reset/mode switch) in the cache: rejected
        # evidence cannot become the basis of a later reconstructed executable quote.
        next_atb = {} if image else self._atb.copy()
        next_batb = {} if image else self._batb.copy()
        if mode == "atb":
            self._apply_atb(raw, next_atb)
        else:
            self._apply_batb(raw, next_batb)

        self._atb = next_atb
        self._batb = next_batb
        self._mode = mode
        if image:
            self._initialized = True
        return AvailableBackUpdate(True, self.current_quote())

    def current_quote(self) -> AvailableBackQuote | None:
        if self._mode == "atb":
            if not self._atb:
                return None
            price = max(self._atb)
            return AvailableBackQuote(
                decimal_odds=price,
                available_size=self._atb[price],
                provider_price_field="rc[].atb",
                ladder_kind="full_price_ladder",
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
                cache_verified=self._initialized,
            )
        return None

    @staticmethod
    def _apply_atb(rows: list[Any], ladder: dict[Decimal, Decimal]) -> None:
        for index, row in enumerate(rows):
            if not isinstance(row, list) or len(row) != 2:
                raise ValueError(f"atb[{index}] must be [price,size]")
            price = _decimal_number(row[0], field=f"atb[{index}].price", positive=True)
            if price <= 1:
                raise ValueError(f"atb[{index}].price must be decimal odds > 1")
            size = _decimal_number(row[1], field=f"atb[{index}].size", non_negative=True)
            if size == 0:
                ladder.pop(price, None)
            else:
                ladder[price] = size

    @staticmethod
    def _apply_batb(rows: list[Any], ladder: dict[int, tuple[Decimal, Decimal]]) -> None:
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
                ladder.pop(level_raw, None)
                continue
            price = _decimal_number(row[1], field=f"batb[{index}].price", positive=True)
            if price <= 1:
                raise ValueError(f"batb[{index}].price must be decimal odds > 1")
            ladder[level_raw] = (price, size)


def _decimal_number(
    value: Any,
    *,
    field: str,
    positive: bool = False,
    non_negative: bool = False,
) -> Decimal:
    # Betfair Stream ladder scalars are JSON numbers. Do not normalize strings or
    # other coercible objects into executable-quote evidence: source type is part of
    # the provider contract even when the rendered decimal text happens to match.
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
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
