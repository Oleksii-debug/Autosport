"""Deterministic Betfair ``listMarketBook`` request budgeting.

This module owns only provider request-shape budgeting and deterministic batch
partitioning.  It performs no I/O, schedules no retries, and grants no
freshness, liquidity, execution, settlement, or real-money authority.

The weight table follows Betfair's documented Market Data Request Limits.
Unsupported projection combinations fail closed rather than guessing a weight.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from hashlib import sha256
import json


class MarketBookRequestBudgetError(ValueError):
    """Raised when a MarketBook request plan cannot be proven provider-safe."""


POLICY_VERSION = "betfair-marketbook-request-budget-planner-v1"
MAX_REQUEST_WEIGHT = Fraction(200, 1)
MAX_CALLS_PER_MARKET_PER_SECOND = 5

_PRICE_DATA_ORDER = (
    "SP_AVAILABLE",
    "SP_TRADED",
    "EX_BEST_OFFERS",
    "EX_ALL_OFFERS",
    "EX_TRADED",
)
_PRICE_DATA_RANK = {value: index for index, value in enumerate(_PRICE_DATA_ORDER)}

# Betfair documents these exact single projections and two special combinations.
# Other multi-projection combinations are deliberately unsupported here rather
# than assigning a guessed sum.
_DOCUMENTED_BASE_WEIGHTS: dict[tuple[str, ...], Fraction] = {
    (): Fraction(2, 1),
    ("SP_AVAILABLE",): Fraction(3, 1),
    ("SP_TRADED",): Fraction(7, 1),
    ("EX_BEST_OFFERS",): Fraction(5, 1),
    ("EX_ALL_OFFERS",): Fraction(17, 1),
    ("EX_TRADED",): Fraction(17, 1),
    ("EX_BEST_OFFERS", "EX_TRADED"): Fraction(20, 1),
    ("EX_ALL_OFFERS", "EX_TRADED"): Fraction(32, 1),
}
_ALLOWED_ORDER_PROJECTIONS = frozenset({"ALL", "EXECUTABLE", "EXECUTION_COMPLETE"})
_ALLOWED_MATCH_PROJECTIONS = frozenset(
    {"NO_ROLLUP", "ROLLED_UP_BY_PRICE", "ROLLED_UP_BY_AVG_PRICE"}
)
_ALLOWED_ROLLUP_MODELS = frozenset({"STAKE", "PAYOUT", "NONE"})
_ALLOWED_MARKET_STATUS_INTENTS = frozenset({"OPEN", "CLOSED"})


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise MarketBookRequestBudgetError(
            f"{field} must be a non-empty trimmed string"
        )
    return value


def _optional_enum(
    value: object,
    field: str,
    allowed: frozenset[str],
) -> str | None:
    if value is None:
        return None
    text = _text(value, field)
    if text not in allowed:
        raise MarketBookRequestBudgetError(
            f"{field} must be one of {', '.join(sorted(allowed))}"
        )
    return text


def _positive_decimal(value: object, field: str) -> Decimal:
    if type(value) is not Decimal or not value.is_finite() or value <= 0:
        raise MarketBookRequestBudgetError(
            f"{field} must be a finite positive Decimal"
        )
    return value


def _canonical_json_sha256(payload: object) -> str:
    try:
        raw = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise MarketBookRequestBudgetError(
            "MarketBook request plan is not canonical JSON"
        ) from exc
    return sha256(raw).hexdigest()


@dataclass(frozen=True, slots=True)
class MarketBookProjection:
    """Exact request-shape inputs that affect budgeting or response semantics."""

    price_data: tuple[str, ...] = ()
    best_prices_depth: int | None = None
    rollup_model: str | None = None
    rollup_limit: Decimal | None = None
    virtualise: bool | None = None
    order_projection: str | None = None
    match_projection: str | None = None

    def __post_init__(self) -> None:
        if type(self.price_data) is not tuple:
            raise MarketBookRequestBudgetError("price_data must be an exact tuple")
        normalized: list[str] = []
        for index, raw in enumerate(self.price_data):
            value = _text(raw, f"price_data[{index}]")
            if value not in _PRICE_DATA_RANK:
                raise MarketBookRequestBudgetError(
                    f"unsupported price_data value: {value}"
                )
            normalized.append(value)
        if len(set(normalized)) != len(normalized):
            raise MarketBookRequestBudgetError("price_data must not contain duplicates")
        canonical = tuple(sorted(normalized, key=_PRICE_DATA_RANK.__getitem__))
        if tuple(normalized) != canonical:
            raise MarketBookRequestBudgetError(
                "price_data must use canonical Betfair projection order"
            )
        if (
            "EX_BEST_OFFERS" in canonical
            and "EX_ALL_OFFERS" in canonical
        ):
            raise MarketBookRequestBudgetError(
                "EX_BEST_OFFERS and EX_ALL_OFFERS cannot be combined in one canonical plan"
            )
        if canonical not in _DOCUMENTED_BASE_WEIGHTS:
            raise MarketBookRequestBudgetError(
                "price_data combination has no documented request weight"
            )

        uses_best_offers = "EX_BEST_OFFERS" in canonical
        has_override = any(
            value is not None
            for value in (
                self.best_prices_depth,
                self.rollup_model,
                self.rollup_limit,
            )
        )
        if has_override and not uses_best_offers:
            raise MarketBookRequestBudgetError(
                "best-offer overrides require EX_BEST_OFFERS"
            )
        if self.best_prices_depth is not None:
            if (
                type(self.best_prices_depth) is not int
                or not 1 <= self.best_prices_depth <= 10
            ):
                raise MarketBookRequestBudgetError(
                    "best_prices_depth must be an exact integer in 1..10"
                )

        rollup_model = _optional_enum(
            self.rollup_model,
            "rollup_model",
            _ALLOWED_ROLLUP_MODELS,
        )
        if rollup_model is None:
            if self.rollup_limit is not None:
                raise MarketBookRequestBudgetError(
                    "rollup_limit requires an explicit rollup_model"
                )
        else:
            if self.rollup_limit is None:
                raise MarketBookRequestBudgetError(
                    "rollup_model requires rollup_limit"
                )
            _positive_decimal(self.rollup_limit, "rollup_limit")

        if self.virtualise is not None:
            if type(self.virtualise) is not bool:
                raise MarketBookRequestBudgetError(
                    "virtualise must be exact bool when supplied"
                )
            if not (
                "EX_BEST_OFFERS" in canonical
                or "EX_ALL_OFFERS" in canonical
            ):
                raise MarketBookRequestBudgetError(
                    "virtualise is only meaningful for exchange-offer projections"
                )

        order_projection = _optional_enum(
            self.order_projection,
            "order_projection",
            _ALLOWED_ORDER_PROJECTIONS,
        )
        match_projection = _optional_enum(
            self.match_projection,
            "match_projection",
            _ALLOWED_MATCH_PROJECTIONS,
        )
        if match_projection is not None and order_projection is None:
            raise MarketBookRequestBudgetError(
                "match_projection requires order_projection"
            )

    @property
    def request_weight(self) -> Fraction:
        base = _DOCUMENTED_BASE_WEIGHTS[self.price_data]
        if "EX_BEST_OFFERS" not in self.price_data:
            return base
        if (
            self.best_prices_depth is None
            and self.rollup_model is None
            and self.rollup_limit is None
        ):
            return base
        requested_depth = (
            3 if self.best_prices_depth is None else self.best_prices_depth
        )
        return base * Fraction(requested_depth, 3)

    @property
    def max_markets_per_request(self) -> int:
        weight = self.request_weight
        if weight <= 0:
            raise MarketBookRequestBudgetError(
                "provider request weight must be positive"
            )
        count = int(MAX_REQUEST_WEIGHT // weight)
        if count < 1:
            raise MarketBookRequestBudgetError(
                "projection exceeds Betfair request budget for one market"
            )
        return count

    def identity_payload(self) -> dict[str, object]:
        return {
            "price_data": list(self.price_data),
            "best_prices_depth": self.best_prices_depth,
            "rollup_model": self.rollup_model,
            "rollup_limit": (
                None if self.rollup_limit is None else str(self.rollup_limit)
            ),
            "virtualise": self.virtualise,
            "order_projection": self.order_projection,
            "match_projection": self.match_projection,
        }


@dataclass(frozen=True, slots=True)
class MarketBookTarget:
    """One exact market requested under an explicit OPEN/CLOSED intent."""

    market_id: str
    status_intent: str

    def __post_init__(self) -> None:
        _text(self.market_id, "market_id")
        if self.status_intent not in _ALLOWED_MARKET_STATUS_INTENTS:
            raise MarketBookRequestBudgetError(
                "status_intent must be OPEN or CLOSED"
            )


@dataclass(frozen=True, slots=True)
class MarketBookBatch:
    """One provider-safe deterministic batch."""

    batch_index: int
    status_intent: str
    market_ids: tuple[str, ...]
    per_market_weight_numerator: int
    per_market_weight_denominator: int
    batch_id: str

    @property
    def per_market_weight(self) -> Fraction:
        return Fraction(
            self.per_market_weight_numerator,
            self.per_market_weight_denominator,
        )

    @property
    def total_weight(self) -> Fraction:
        return self.per_market_weight * len(self.market_ids)


@dataclass(frozen=True, slots=True)
class MarketBookReadPlan:
    """Immutable deterministic partition of a requested MarketBook read set."""

    policy_version: str
    projection: MarketBookProjection
    targets: tuple[MarketBookTarget, ...]
    batches: tuple[MarketBookBatch, ...]
    plan_id: str

    @property
    def market_ids(self) -> tuple[str, ...]:
        return tuple(target.market_id for target in self.targets)


def plan_market_book_reads(
    targets: tuple[MarketBookTarget, ...],
    projection: MarketBookProjection,
) -> MarketBookReadPlan:
    """Return a deterministic, provider-budget-safe batch plan.

    Input order is authority-bearing and preserved.  A batch is also cut when
    OPEN/CLOSED intent changes, because Betfair documents that those statuses
    must be requested separately.
    """

    if type(targets) is not tuple or not targets:
        raise MarketBookRequestBudgetError(
            "targets must be a non-empty exact tuple"
        )
    if type(projection) is not MarketBookProjection:
        raise MarketBookRequestBudgetError(
            "projection must be an exact MarketBookProjection"
        )
    seen: set[str] = set()
    for index, target in enumerate(targets):
        if type(target) is not MarketBookTarget:
            raise MarketBookRequestBudgetError(
                f"targets[{index}] must be an exact MarketBookTarget"
            )
        if target.market_id in seen:
            raise MarketBookRequestBudgetError(
                f"duplicate market_id in request plan: {target.market_id}"
            )
        seen.add(target.market_id)

    max_markets = projection.max_markets_per_request
    per_market_weight = projection.request_weight
    projection_payload = projection.identity_payload()

    raw_batches: list[tuple[str, tuple[str, ...]]] = []
    current_status: str | None = None
    current_ids: list[str] = []
    for target in targets:
        if (
            current_ids
            and (
                target.status_intent != current_status
                or len(current_ids) >= max_markets
            )
        ):
            raw_batches.append((current_status, tuple(current_ids)))  # type: ignore[arg-type]
            current_ids = []
        if not current_ids:
            current_status = target.status_intent
        current_ids.append(target.market_id)
    if current_ids:
        raw_batches.append((current_status, tuple(current_ids)))  # type: ignore[arg-type]

    batches: list[MarketBookBatch] = []
    for batch_index, (status_intent, market_ids) in enumerate(raw_batches):
        total_weight = per_market_weight * len(market_ids)
        if total_weight > MAX_REQUEST_WEIGHT:
            raise MarketBookRequestBudgetError(
                "internal planning error: batch exceeds provider request budget"
            )
        batch_payload = {
            "schema": "autosport.betfair_marketbook_batch",
            "schema_version": 1,
            "policy_version": POLICY_VERSION,
            "batch_index": batch_index,
            "status_intent": status_intent,
            "market_ids": list(market_ids),
            "projection": projection_payload,
            "per_market_weight": {
                "numerator": per_market_weight.numerator,
                "denominator": per_market_weight.denominator,
            },
        }
        batches.append(
            MarketBookBatch(
                batch_index=batch_index,
                status_intent=status_intent,
                market_ids=market_ids,
                per_market_weight_numerator=per_market_weight.numerator,
                per_market_weight_denominator=per_market_weight.denominator,
                batch_id=_canonical_json_sha256(batch_payload),
            )
        )

    plan_payload = {
        "schema": "autosport.betfair_marketbook_read_plan",
        "schema_version": 1,
        "policy_version": POLICY_VERSION,
        "projection": projection_payload,
        "targets": [
            {
                "market_id": target.market_id,
                "status_intent": target.status_intent,
            }
            for target in targets
        ],
        "batches": [
            {
                "batch_index": batch.batch_index,
                "status_intent": batch.status_intent,
                "market_ids": list(batch.market_ids),
                "per_market_weight": {
                    "numerator": batch.per_market_weight_numerator,
                    "denominator": batch.per_market_weight_denominator,
                },
                "batch_id": batch.batch_id,
            }
            for batch in batches
        ],
    }
    return MarketBookReadPlan(
        policy_version=POLICY_VERSION,
        projection=projection,
        targets=targets,
        batches=tuple(batches),
        plan_id=_canonical_json_sha256(plan_payload),
    )
