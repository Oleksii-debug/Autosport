"""Deterministic, transport-free Betfair listMarketBook batch planning."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Mapping, Sequence

from .betfair_marketbook_request_budget import (
    MAX_REQUEST_POINTS,
    MarketBookBudgetError,
    MarketBookRequestBudget,
)

MAX_COMBINED_IDENTIFIERS = 250
PLAN_POLICY_VERSION = "betfair-marketbook-batch-plan-v1"
_ORDER = {"EXECUTABLE", "EXECUTION_COMPLETE", "ALL"}
_MATCH = {"NO_ROLLUP", "ROLLED_UP_BY_PRICE", "ROLLED_UP_BY_AVG_PRICE"}


class MarketBookBatchPlanError(ValueError):
    """The requested batch plan cannot be proven canonically."""


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha(value: object) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _token(value: object, name: str, *, optional: bool = True) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value or value != value.strip():
        raise MarketBookBatchPlanError(f"{name} must be a non-empty canonical string")
    return value


def _tokens(values: object, name: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise MarketBookBatchPlanError(f"{name} must be a sequence of strings")
    items = tuple(_token(item, name, optional=False) for item in values)
    if len(items) != len(set(items)):
        raise MarketBookBatchPlanError(f"{name} contains duplicates")
    return tuple(sorted(items))  # type: ignore[arg-type]


def _bool(value: object, name: str) -> bool | None:
    if value is not None and not isinstance(value, bool):
        raise MarketBookBatchPlanError(f"{name} must be bool or None")
    return value


def _positive_int(value: object, name: str) -> int | None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
        raise MarketBookBatchPlanError(f"{name} must be a positive integer or None")
    return value


def _fraction(value) -> dict[str, int]:
    return {"numerator": value.numerator, "denominator": value.denominator}


@dataclass(frozen=True, slots=True)
class MarketBookReadBatch:
    index: int
    market_ids: tuple[str, ...]
    budget_evidence_id: str
    weight_per_market: dict[str, int]
    total_points: dict[str, int]
    combined_identifier_count: int
    batch_id: str

    def payload(self) -> dict[str, object]:
        return {
            "index": self.index,
            "market_ids": list(self.market_ids),
            "budget_evidence_id": self.budget_evidence_id,
            "weight_per_market": self.weight_per_market,
            "total_points": self.total_points,
            "combined_identifier_count": self.combined_identifier_count,
            "batch_id": self.batch_id,
        }


@dataclass(frozen=True, slots=True)
class MarketBookReadPlan:
    """Complete batch partition; never accepts caller-authored budget evidence."""

    market_ids: tuple[str, ...]
    market_status: str
    price_data: tuple[str, ...] = ()
    best_prices_depth: int | None = None
    virtualise: bool | None = None
    rollover_stakes: bool | None = None
    order_projection: str | None = None
    match_projection: str | None = None
    include_overall_position: bool | None = None
    partition_matched_by_strategy_ref: bool | None = None
    customer_strategy_refs: tuple[str, ...] = ()
    matched_since: str | None = None
    bet_ids: tuple[str, ...] = ()
    currency_code: str | None = None
    locale: str | None = None
    rollup_model: str | None = None
    rollup_limit: int | None = None
    provider_scope_id: str = "BETFAIR"
    policy_version: str = PLAN_POLICY_VERSION

    def __post_init__(self) -> None:
        state = self._state()
        for name, value in state.items():
            object.__setattr__(self, name, value)
        self._batches()

    def _state(self) -> dict[str, object]:
        market_ids = _tokens(self.market_ids, "market_ids")
        if not market_ids:
            raise MarketBookBatchPlanError("market_ids must contain at least one market")
        status = _token(self.market_status, "market_status", optional=False)
        if status not in {"OPEN", "CLOSED"}:
            raise MarketBookBatchPlanError("market_status must be OPEN or CLOSED")
        try:
            probe = MarketBookRequestBudget(
                (market_ids[0],), tuple(self.price_data), self.best_prices_depth, "listMarketBook"
            )
        except MarketBookBudgetError as exc:
            raise MarketBookBatchPlanError(str(exc)) from exc
        effective_price_data = set(probe.effective_price_data)
        virtualise = _bool(self.virtualise, "virtualise")
        rollover_stakes = _bool(self.rollover_stakes, "rollover_stakes")
        if (virtualise is not None or rollover_stakes is not None) and not effective_price_data.intersection({"EX_BEST_OFFERS", "EX_ALL_OFFERS"}):
            raise MarketBookBatchPlanError("price projection flags require an exchange-offers price projection")
        rollup_model = _token(self.rollup_model, "rollup_model")
        rollup_limit = _positive_int(self.rollup_limit, "rollup_limit")
        if rollup_model is not None and rollup_model not in {"STAKE", "PAYOUT", "NONE"}:
            raise MarketBookBatchPlanError("unsupported rollup_model")
        if rollup_model is not None and rollup_limit is None:
            raise MarketBookBatchPlanError("rollup_limit is required when rollup_model is specified")
        if rollup_model is None and rollup_limit is not None:
            raise MarketBookBatchPlanError("rollup_limit without rollup_model is ignored by Betfair")
        if (rollup_model is not None or rollup_limit is not None) and "EX_BEST_OFFERS" not in effective_price_data:
            raise MarketBookBatchPlanError("rollup overrides require effective EX_BEST_OFFERS")
        order = _token(self.order_projection, "order_projection")
        match = _token(self.match_projection, "match_projection")
        if order is not None and order not in _ORDER:
            raise MarketBookBatchPlanError("unsupported order_projection")
        if match is not None and match not in _MATCH:
            raise MarketBookBatchPlanError("unsupported match_projection")

        include_overall_position = _bool(
            self.include_overall_position,
            "include_overall_position",
        )
        partition_matched_by_strategy_ref = _bool(
            self.partition_matched_by_strategy_ref,
            "partition_matched_by_strategy_ref",
        )
        customer_strategy_refs = _tokens(
            self.customer_strategy_refs,
            "customer_strategy_refs",
        )
        matched_since = _token(self.matched_since, "matched_since")
        bet_ids = _tokens(self.bet_ids, "bet_ids")
        if order is None:
            order_only_fields_present = (
                match is not None
                or include_overall_position is not None
                or partition_matched_by_strategy_ref is not None
                or bool(customer_strategy_refs)
                or matched_since is not None
                or bool(bet_ids)
            )
            if order_only_fields_present:
                raise MarketBookBatchPlanError(
                    "order-only fields require order_projection"
                )
        else:
            if include_overall_position is None:
                include_overall_position = True
            if partition_matched_by_strategy_ref is None:
                partition_matched_by_strategy_ref = False

        if len(bet_ids) >= MAX_COMBINED_IDENTIFIERS:
            raise MarketBookBatchPlanError(
                "bet_ids leave no identifier capacity for a listMarketBook market_id"
            )
        return {
            "market_ids": market_ids,
            "market_status": status,
            "price_data": probe.price_data,
            "best_prices_depth": self.best_prices_depth,
            "virtualise": virtualise,
            "rollover_stakes": rollover_stakes,
            "order_projection": order,
            "match_projection": match,
            "include_overall_position": include_overall_position,
            "partition_matched_by_strategy_ref": partition_matched_by_strategy_ref,
            "customer_strategy_refs": customer_strategy_refs,
            "matched_since": matched_since,
            "bet_ids": bet_ids,
            "currency_code": _token(self.currency_code, "currency_code"),
            "locale": _token(self.locale, "locale"),
            "rollup_model": rollup_model,
            "rollup_limit": rollup_limit,
            "provider_scope_id": _token(self.provider_scope_id, "provider_scope_id", optional=False),
            "policy_version": _token(self.policy_version, "policy_version", optional=False),
        }

    @property
    def request_contract_payload(self) -> dict[str, object]:
        s = self._state()
        return {
            "provider": "BETFAIR",
            "operation": "listMarketBook",
            "market_status": s["market_status"],
            "market_ids": list(s["market_ids"]),
            "price_data": list(s["price_data"]),
            "best_prices_depth": s["best_prices_depth"],
            "virtualise": s["virtualise"],
            "rollover_stakes": s["rollover_stakes"],
            "order_projection": s["order_projection"],
            "match_projection": s["match_projection"],
            "include_overall_position": s["include_overall_position"],
            "partition_matched_by_strategy_ref": s["partition_matched_by_strategy_ref"],
            "customer_strategy_refs": list(s["customer_strategy_refs"]),
            "matched_since": s["matched_since"],
            "bet_ids": list(s["bet_ids"]),
            "currency_code": s["currency_code"],
            "locale": s["locale"],
            "ex_best_offers_overrides": {
                "rollup_model": s["rollup_model"],
                "rollup_limit": s["rollup_limit"],
            },
            "provider_scope_id": s["provider_scope_id"],
            "policy_version": s["policy_version"],
        }

    @property
    def request_contract_id(self) -> str:
        return _sha(self.request_contract_payload)

    def _capacity(self, s: dict[str, object]) -> int:
        try:
            probe = MarketBookRequestBudget(
                (s["market_ids"][0],), s["price_data"], s["best_prices_depth"], "listMarketBook"
            )
        except MarketBookBudgetError as exc:
            raise MarketBookBatchPlanError(str(exc)) from exc
        weight_capacity = int(MAX_REQUEST_POINTS // probe.weight_per_market)
        identifier_capacity = MAX_COMBINED_IDENTIFIERS - len(s["bet_ids"])
        capacity = min(weight_capacity, identifier_capacity)
        if capacity < 1:
            raise MarketBookBatchPlanError("request semantics leave no safe market batch capacity")
        return capacity

    def _batches(self) -> tuple[MarketBookReadBatch, ...]:
        s = self._state()
        market_ids = s["market_ids"]
        capacity = self._capacity(s)
        contract_id = _sha(self.request_contract_payload)
        result: list[MarketBookReadBatch] = []
        for index, start in enumerate(range(0, len(market_ids), capacity)):
            chunk = market_ids[start : start + capacity]
            try:
                budget = MarketBookRequestBudget(
                    chunk, s["price_data"], s["best_prices_depth"], "listMarketBook"
                )
            except MarketBookBudgetError as exc:
                raise MarketBookBatchPlanError(str(exc)) from exc
            combined = len(chunk) + len(s["bet_ids"])
            if not budget.allowed or combined > MAX_COMBINED_IDENTIFIERS:
                raise MarketBookBatchPlanError("derived batch violates canonical provider limits")
            core = {
                "request_contract_id": contract_id,
                "index": index,
                "market_ids": list(chunk),
                "budget_evidence_id": budget.evidence_id,
                "combined_identifier_count": combined,
            }
            result.append(
                MarketBookReadBatch(
                    index,
                    chunk,
                    budget.evidence_id,
                    _fraction(budget.weight_per_market),
                    _fraction(budget.total_points),
                    combined,
                    _sha(core),
                )
            )
        flattened = tuple(mid for batch in result for mid in batch.market_ids)
        if flattened != market_ids or len(flattened) != len(set(flattened)):
            raise MarketBookBatchPlanError("derived batches do not cover the exact market universe")
        return tuple(result)

    @property
    def batches(self) -> tuple[MarketBookReadBatch, ...]:
        return self._batches()

    @property
    def evidence_payload(self) -> dict[str, object]:
        return {
            "schema": PLAN_POLICY_VERSION,
            "request_contract": self.request_contract_payload,
            "request_contract_id": self.request_contract_id,
            "max_request_points": MAX_REQUEST_POINTS,
            "max_combined_identifiers": MAX_COMBINED_IDENTIFIERS,
            "batches": [batch.payload() for batch in self.batches],
            "complete_market_coverage": True,
            "provider_dispatch_authorized": False,
            "execution_authorized": False,
        }

    @property
    def plan_id(self) -> str:
        return _sha(self.evidence_payload)

    def to_json(self) -> str:
        return _json({"evidence": self.evidence_payload, "plan_id": self.plan_id})

    @classmethod
    def from_json(cls, encoded: str) -> "MarketBookReadPlan":
        try:
            envelope = json.loads(encoded) if isinstance(encoded, str) else None
        except json.JSONDecodeError as exc:
            raise MarketBookBatchPlanError("encoded plan is not valid JSON") from exc
        if not isinstance(envelope, Mapping) or set(envelope) != {"evidence", "plan_id"}:
            raise MarketBookBatchPlanError("encoded plan has a noncanonical envelope")
        evidence = envelope["evidence"]
        if not isinstance(evidence, Mapping):
            raise MarketBookBatchPlanError("encoded plan evidence must be an object")
        c = evidence.get("request_contract")
        if not isinstance(c, Mapping):
            raise MarketBookBatchPlanError("encoded plan is missing request_contract")
        o = c.get("ex_best_offers_overrides")
        if not isinstance(o, Mapping):
            raise MarketBookBatchPlanError("encoded plan has invalid best-offer overrides")
        try:
            restored = cls(
                market_ids=tuple(c.get("market_ids", ())),
                market_status=c.get("market_status"),
                price_data=tuple(c.get("price_data", ())),
                best_prices_depth=c.get("best_prices_depth"),
                virtualise=c.get("virtualise"),
                rollover_stakes=c.get("rollover_stakes"),
                order_projection=c.get("order_projection"),
                match_projection=c.get("match_projection"),
                include_overall_position=c.get("include_overall_position"),
                partition_matched_by_strategy_ref=c.get("partition_matched_by_strategy_ref"),
                customer_strategy_refs=tuple(c.get("customer_strategy_refs", ())),
                matched_since=c.get("matched_since"),
                bet_ids=tuple(c.get("bet_ids", ())),
                currency_code=c.get("currency_code"),
                locale=c.get("locale"),
                rollup_model=o.get("rollup_model"),
                rollup_limit=o.get("rollup_limit"),
                provider_scope_id=c.get("provider_scope_id"),
                policy_version=c.get("policy_version"),
            )
        except (TypeError, MarketBookBatchPlanError) as exc:
            raise MarketBookBatchPlanError("encoded plan request contract is invalid") from exc
        if restored.evidence_payload != evidence or restored.plan_id != envelope["plan_id"]:
            raise MarketBookBatchPlanError("encoded plan does not match canonical recomputation")
        return restored
