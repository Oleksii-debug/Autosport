from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Iterable

PROVIDER_ID = "matchbook"
SCHEMA_VERSION = 1

_PRICE_MODES = frozenset({"expanded", "aggregated"})
_SIDE_SCOPES = frozenset({"back", "lay", "both"})
_PRICE_SIDES = frozenset({"back", "lay"})
_HEX = frozenset("0123456789abcdef")

RETURNED_PRICE = "RETURNED_PRICE"
NO_PRICE_WITHIN_FILTERED_SCOPE = "NO_PRICE_RETURNED_WITHIN_FILTERED_REQUEST_SCOPE"
OUTSIDE_SIDE_SCOPE = "OUTSIDE_REQUEST_SIDE_SCOPE"
OUTSIDE_DEPTH_SCOPE = "OUTSIDE_REQUEST_DEPTH_SCOPE"

EXACT_RETURNED_LEVEL = "EXACT_RETURNED_LEVEL"
AGGREGATE_TAIL_LEVEL = "AGGREGATE_TAIL_LEVEL"


class MatchbookPriceEvidenceError(ValueError):
    """Matchbook price evidence violates the bounded request-censoring contract."""


def _token(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 200
        or any(ord(ch) < 33 or ord(ch) > 126 for ch in value)
    ):
        raise MatchbookPriceEvidenceError(
            f"{field} must be a trimmed printable-ASCII token of at most 200 characters"
        )
    return value


def _positive_int(value: object, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise MatchbookPriceEvidenceError(
            f"{field} must be a positive non-boolean integer"
        )
    return value


def _decimal_exact(
    value: object,
    field: str,
    *,
    minimum: Decimal,
    strict_greater: bool = False,
) -> Decimal:
    if type(value) is not Decimal:
        raise MatchbookPriceEvidenceError(f"{field} must be an exact Decimal")
    if not value.is_finite():
        raise MatchbookPriceEvidenceError(f"{field} must be finite")
    if strict_greater:
        if value <= minimum:
            raise MatchbookPriceEvidenceError(f"{field} must be greater than {minimum}")
    elif value < minimum:
        raise MatchbookPriceEvidenceError(f"{field} must be at least {minimum}")
    return value


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _currency_code(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 3
        or value != value.upper()
        or any(ch < "A" or ch > "Z" for ch in value)
    ):
        raise MatchbookPriceEvidenceError(
            "currency_code must be a three-letter uppercase ASCII code"
        )
    return value


def _require_utc(value: object, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise MatchbookPriceEvidenceError(f"{field} must be datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise MatchbookPriceEvidenceError(f"{field} must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise MatchbookPriceEvidenceError(f"{field} must use UTC offset +00:00")
    return value


def _utc_text(value: datetime) -> str:
    _require_utc(value, "observed_at")
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _sha256_hex(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or value != value.lower()
        or any(ch not in _HEX for ch in value)
    ):
        raise MatchbookPriceEvidenceError(f"{field} must be lowercase SHA-256 hex")
    return value


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class MatchbookPriceRequestScope:
    """Exact request-shaping semantics that censor a Matchbook price view.

    ``minimum_liquidity=None`` means the acquisition used the provider default.
    In that case a provider policy reference is mandatory so a later provider-default
    change cannot silently reuse the same feature/request semantics.
    """

    price_depth: int
    price_mode: str
    minimum_liquidity: Decimal | None
    provider_default_policy_ref: str | None
    side_scope: str
    exclude_mirrored_prices: bool
    currency_code: str
    odds_type: str = "DECIMAL"
    exchange_type: str = "back-lay"

    def __post_init__(self) -> None:
        _positive_int(self.price_depth, "price_depth")
        if self.price_mode not in _PRICE_MODES:
            raise MatchbookPriceEvidenceError(
                "price_mode must be 'expanded' or 'aggregated'"
            )
        if self.side_scope not in _SIDE_SCOPES:
            raise MatchbookPriceEvidenceError(
                "side_scope must be 'back', 'lay', or 'both'"
            )
        if type(self.exclude_mirrored_prices) is not bool:
            raise MatchbookPriceEvidenceError(
                "exclude_mirrored_prices must be bool"
            )
        _currency_code(self.currency_code)
        if self.odds_type != "DECIMAL":
            raise MatchbookPriceEvidenceError("odds_type must be exactly DECIMAL")
        if self.exchange_type != "back-lay":
            raise MatchbookPriceEvidenceError(
                "exchange_type must be exactly back-lay"
            )

        if self.minimum_liquidity is None:
            if self.provider_default_policy_ref is None:
                raise MatchbookPriceEvidenceError(
                    "provider_default_policy_ref is required when "
                    "minimum_liquidity is omitted"
                )
            _token(
                self.provider_default_policy_ref,
                "provider_default_policy_ref",
            )
        else:
            _decimal_exact(
                self.minimum_liquidity,
                "minimum_liquidity",
                minimum=Decimal("0"),
            )
            if self.provider_default_policy_ref is not None:
                raise MatchbookPriceEvidenceError(
                    "provider_default_policy_ref must be absent when "
                    "minimum_liquidity is explicit"
                )

    @property
    def maximum_visible_levels_per_side(self) -> int:
        if self.price_mode == "aggregated":
            return min(self.price_depth, 3)
        return self.price_depth

    def includes_side(self, side: str) -> bool:
        if side not in _PRICE_SIDES:
            raise MatchbookPriceEvidenceError("side must be 'back' or 'lay'")
        return self.side_scope == "both" or self.side_scope == side

    def projection(self) -> dict[str, object]:
        return {
            "price_depth": self.price_depth,
            "price_mode": self.price_mode,
            "minimum_liquidity": (
                None
                if self.minimum_liquidity is None
                else _decimal_text(self.minimum_liquidity)
            ),
            "minimum_liquidity_explicit": self.minimum_liquidity is not None,
            "provider_default_policy_ref": self.provider_default_policy_ref,
            "side_scope": self.side_scope,
            "exclude_mirrored_prices": self.exclude_mirrored_prices,
            "currency_code": self.currency_code,
            "odds_type": self.odds_type,
            "exchange_type": self.exchange_type,
        }

    @property
    def scope_sha256(self) -> str:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "provider_id": PROVIDER_ID,
            "request_scope": self.projection(),
        }
        return _sha256_text(_canonical_json(payload))


@dataclass(frozen=True, slots=True)
class MatchbookObservedPrice:
    side: str
    level: int
    decimal_odds: Decimal
    available_amount: Decimal

    def __post_init__(self) -> None:
        if self.side not in _PRICE_SIDES:
            raise MatchbookPriceEvidenceError("side must be 'back' or 'lay'")
        _positive_int(self.level, "level")
        _decimal_exact(
            self.decimal_odds,
            "decimal_odds",
            minimum=Decimal("1"),
            strict_greater=True,
        )
        _decimal_exact(
            self.available_amount,
            "available_amount",
            minimum=Decimal("0"),
            strict_greater=True,
        )

    def projection(self, *, aggregate_tail: bool) -> dict[str, object]:
        return {
            "side": self.side,
            "level": self.level,
            "decimal_odds": _decimal_text(self.decimal_odds),
            "available_amount": _decimal_text(self.available_amount),
            "level_semantics": (
                AGGREGATE_TAIL_LEVEL if aggregate_tail else EXACT_RETURNED_LEVEL
            ),
        }


@dataclass(frozen=True, slots=True)
class MatchbookPriceSnapshotEvidence:
    """Immutable evidence for one provider-native runner price snapshot.

    This object records what request-shaped price view was observed. It never
    converts a missing row into proof that the market had no liquidity.
    """

    account_scope_ref: str
    session_generation: int
    event_id: int
    market_id: int
    runner_id: int
    request_scope: MatchbookPriceRequestScope
    observed_at: datetime
    raw_response_sha256: str
    raw_response_size_bytes: int
    prices: tuple[MatchbookObservedPrice, ...]

    def __post_init__(self) -> None:
        _token(self.account_scope_ref, "account_scope_ref")
        _positive_int(self.session_generation, "session_generation")
        _positive_int(self.event_id, "event_id")
        _positive_int(self.market_id, "market_id")
        _positive_int(self.runner_id, "runner_id")
        if not isinstance(self.request_scope, MatchbookPriceRequestScope):
            raise MatchbookPriceEvidenceError(
                "request_scope must be MatchbookPriceRequestScope"
            )
        _require_utc(self.observed_at, "observed_at")
        _sha256_hex(self.raw_response_sha256, "raw_response_sha256")
        _positive_int(self.raw_response_size_bytes, "raw_response_size_bytes")
        if type(self.prices) is not tuple:
            raise MatchbookPriceEvidenceError("prices must be a tuple")
        if any(not isinstance(item, MatchbookObservedPrice) for item in self.prices):
            raise MatchbookPriceEvidenceError(
                "prices must contain MatchbookObservedPrice values"
            )
        self._validate_returned_levels()

    def _validate_returned_levels(self) -> None:
        seen: set[tuple[str, int]] = set()
        levels_by_side: dict[str, list[int]] = {"back": [], "lay": []}
        maximum = self.request_scope.maximum_visible_levels_per_side

        for price in self.prices:
            if not self.request_scope.includes_side(price.side):
                raise MatchbookPriceEvidenceError(
                    "returned price escaped the requested side scope"
                )
            if price.level > maximum:
                raise MatchbookPriceEvidenceError(
                    "returned price escaped the requested depth/mode scope"
                )
            key = (price.side, price.level)
            if key in seen:
                raise MatchbookPriceEvidenceError(
                    "duplicate returned price side/level identity"
                )
            seen.add(key)
            levels_by_side[price.side].append(price.level)

        for side, levels in levels_by_side.items():
            if not levels:
                continue
            ordered = sorted(levels)
            if ordered != list(range(1, ordered[-1] + 1)):
                raise MatchbookPriceEvidenceError(
                    f"{side} returned price levels must be contiguous from level 1"
                )

    @property
    def provider_id(self) -> str:
        return PROVIDER_ID

    @property
    def observed_at_utc(self) -> str:
        return _utc_text(self.observed_at)

    def _aggregate_tail(self, price: MatchbookObservedPrice) -> bool:
        return self.request_scope.price_mode == "aggregated" and price.level == 3

    def ordered_prices(self) -> tuple[MatchbookObservedPrice, ...]:
        side_order = {"back": 0, "lay": 1}
        return tuple(sorted(self.prices, key=lambda p: (side_order[p.side], p.level)))

    def price_projection(self) -> list[dict[str, object]]:
        return [
            price.projection(aggregate_tail=self._aggregate_tail(price))
            for price in self.ordered_prices()
        ]

    def price_at(self, side: str, level: int) -> MatchbookObservedPrice | None:
        if side not in _PRICE_SIDES:
            raise MatchbookPriceEvidenceError("side must be 'back' or 'lay'")
        _positive_int(level, "level")
        for price in self.prices:
            if price.side == side and price.level == level:
                return price
        return None

    def absence_truth(self, side: str, level: int) -> str:
        if side not in _PRICE_SIDES:
            raise MatchbookPriceEvidenceError("side must be 'back' or 'lay'")
        _positive_int(level, "level")
        if not self.request_scope.includes_side(side):
            return OUTSIDE_SIDE_SCOPE
        if level > self.request_scope.maximum_visible_levels_per_side:
            return OUTSIDE_DEPTH_SCOPE
        if self.price_at(side, level) is not None:
            return RETURNED_PRICE
        return NO_PRICE_WITHIN_FILTERED_SCOPE

    def level_semantics(self, side: str, level: int) -> str | None:
        price = self.price_at(side, level)
        if price is None:
            return None
        if self._aggregate_tail(price):
            return AGGREGATE_TAIL_LEVEL
        return EXACT_RETURNED_LEVEL

    @property
    def request_scope_sha256(self) -> str:
        return self.request_scope.scope_sha256

    @property
    def evidence_sha256(self) -> str:
        return _sha256_text(_canonical_json(self.projection(include_evidence_id=False)))

    def projection(self, *, include_evidence_id: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "provider_id": PROVIDER_ID,
            "account_scope_ref": self.account_scope_ref,
            "session_generation": self.session_generation,
            "event_id": self.event_id,
            "market_id": self.market_id,
            "runner_id": self.runner_id,
            "request_scope": self.request_scope.projection(),
            "request_scope_sha256": self.request_scope_sha256,
            "observed_at_utc": self.observed_at_utc,
            "raw_response_sha256": self.raw_response_sha256,
            "raw_response_size_bytes": self.raw_response_size_bytes,
            "prices": self.price_projection(),
            "proves_market_has_no_liquidity": False,
            "parsed_rows_bound_to_raw_response": False,
            "grants_provider_acquisition_authority": False,
            "grants_execution_authority": False,
            "grants_settlement_authority": False,
            "grants_strategy_promotion_authority": False,
        }
        if include_evidence_id:
            payload["evidence_sha256"] = self.evidence_sha256
        return payload


def build_matchbook_price_snapshot_evidence(
    *,
    account_scope_ref: str,
    session_generation: int,
    event_id: int,
    market_id: int,
    runner_id: int,
    request_scope: MatchbookPriceRequestScope,
    observed_at: datetime,
    raw_response: bytes,
    prices: Iterable[MatchbookObservedPrice],
) -> MatchbookPriceSnapshotEvidence:
    if type(raw_response) is not bytes or not raw_response:
        raise MatchbookPriceEvidenceError("raw_response must be non-empty bytes")
    return MatchbookPriceSnapshotEvidence(
        account_scope_ref=account_scope_ref,
        session_generation=session_generation,
        event_id=event_id,
        market_id=market_id,
        runner_id=runner_id,
        request_scope=request_scope,
        observed_at=observed_at,
        raw_response_sha256=hashlib.sha256(raw_response).hexdigest(),
        raw_response_size_bytes=len(raw_response),
        prices=tuple(prices),
    )
