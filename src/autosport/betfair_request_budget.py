"""Deterministic Betfair request-budget and provider-pressure contract.

The contract is intentionally transport-free. It decides whether a *proposed* provider
request may consume a bounded request budget; it never performs network access, stores
credentials, grants provider-write authority, or changes execution truth.

The hard provider limits represented here are deliberately narrow:
- one listMarketBook request must stay within 200 weighted market-data points;
- one market must not be dispatched through listMarketBook more than five times in
  a rolling one-second window.

Concurrency limits are product policy, not claimed provider constants, so callers must
provide them explicitly. Reconciliation capacity is reserved before ordinary traffic.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from fractions import Fraction
from threading import Lock
from typing import Iterable

from . import betfair_marketbook_request_budget as _marketbook_budget


_ONE_SECOND_NS = 1_000_000_000
_MAX_MARKET_BOOK_PER_MARKET_PER_SECOND = 5
_ORDER_PROJECTIONS = frozenset({"ALL", "EXECUTABLE", "EXECUTION_COMPLETE"})
_MATCH_PROJECTIONS = frozenset(
    {"NO_ROLLUP", "ROLLED_UP_BY_PRICE", "ROLLED_UP_BY_AVG_PRICE"}
)


class BetfairRequestBudgetError(ValueError):
    """Raised when a request-budget object violates the bounded contract."""


class BetfairRequestPriority(str, Enum):
    RECONCILIATION = "reconciliation"
    SAFETY = "safety"
    EXECUTION_READ = "execution_read"
    EXECUTION_MUTATION = "execution_mutation"
    MONITORING = "monitoring"
    BACKGROUND = "background"

    @property
    def rank(self) -> int:
        return {
            BetfairRequestPriority.RECONCILIATION: 0,
            BetfairRequestPriority.SAFETY: 1,
            BetfairRequestPriority.EXECUTION_READ: 2,
            BetfairRequestPriority.EXECUTION_MUTATION: 3,
            BetfairRequestPriority.MONITORING: 4,
            BetfairRequestPriority.BACKGROUND: 5,
        }[self]


class BetfairRequestOperation(str, Enum):
    LIST_MARKET_BOOK = "list_market_book"
    LIST_CURRENT_ORDERS = "list_current_orders"
    LIST_MARKET_PROFIT_AND_LOSS = "list_market_profit_and_loss"
    LIST_CLEARED_ORDERS = "list_cleared_orders"
    PLACE_ORDERS = "place_orders"
    CANCEL_ORDERS = "cancel_orders"
    UPDATE_ORDERS = "update_orders"
    REPLACE_ORDERS = "replace_orders"


class BetfairRequestPool(str, Enum):
    MARKET_DATA = "market_data"
    SHARED_ORDER_READ = "shared_order_read"
    CLEARED_ORDERS = "cleared_orders"
    MUTATION = "mutation"


class BetfairAdmissionDecision(str, Enum):
    ADMIT = "admit"
    THROTTLE = "throttle"
    COALESCE = "coalesce"
    DEFER_STREAM_SUFFICIENT = "defer_stream_sufficient"
    BLOCK_RECONCILIATION_REQUIRED = "block_reconciliation_required"


class BetfairStreamState(str, Enum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"


_READ_OPERATIONS = frozenset(
    {
        BetfairRequestOperation.LIST_MARKET_BOOK,
        BetfairRequestOperation.LIST_CURRENT_ORDERS,
        BetfairRequestOperation.LIST_MARKET_PROFIT_AND_LOSS,
        BetfairRequestOperation.LIST_CLEARED_ORDERS,
    }
)
_MUTATION_OPERATIONS = frozenset(
    {
        BetfairRequestOperation.PLACE_ORDERS,
        BetfairRequestOperation.CANCEL_ORDERS,
        BetfairRequestOperation.UPDATE_ORDERS,
        BetfairRequestOperation.REPLACE_ORDERS,
    }
)
_RECONCILIATION_OPERATIONS = frozenset(
    {
        BetfairRequestOperation.LIST_CURRENT_ORDERS,
        BetfairRequestOperation.LIST_CLEARED_ORDERS,
    }
)
_BLOCKED_WHILE_MUTATION_UNKNOWN = frozenset(
    {
        BetfairRequestOperation.PLACE_ORDERS,
        BetfairRequestOperation.UPDATE_ORDERS,
        BetfairRequestOperation.REPLACE_ORDERS,
    }
)


def _exact_text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise BetfairRequestBudgetError(
            f"{field} must be a non-empty trimmed exact string"
        )
    return value


def _nonnegative_int(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise BetfairRequestBudgetError(
            f"{field} must be a non-negative exact integer"
        )
    return value


def _positive_int(value: object, field: str) -> int:
    value = _nonnegative_int(value, field)
    if value == 0:
        raise BetfairRequestBudgetError(
            f"{field} must be a positive exact integer"
        )
    return value


@dataclass(frozen=True, slots=True)
class BetfairRequestBudgetPolicy:
    """Product-owned concurrency/backoff policy.

    The pending limits below are explicitly policy inputs. They are not assertions
    about undocumented provider constants.
    """

    shared_order_read_pending_limit: int
    shared_order_read_reconciliation_reserve: int
    cleared_orders_pending_limit: int
    market_data_pending_limit: int
    mutation_pending_limit: int
    read_backoff_base_ms: int
    read_backoff_max_ms: int

    def __post_init__(self) -> None:
        shared = _positive_int(
            self.shared_order_read_pending_limit,
            "shared_order_read_pending_limit",
        )
        reserve = _positive_int(
            self.shared_order_read_reconciliation_reserve,
            "shared_order_read_reconciliation_reserve",
        )
        if reserve >= shared:
            raise BetfairRequestBudgetError(
                "shared_order_read_reconciliation_reserve must be smaller than "
                "shared_order_read_pending_limit"
            )
        _positive_int(self.cleared_orders_pending_limit, "cleared_orders_pending_limit")
        _positive_int(self.market_data_pending_limit, "market_data_pending_limit")
        _positive_int(self.mutation_pending_limit, "mutation_pending_limit")
        base = _positive_int(self.read_backoff_base_ms, "read_backoff_base_ms")
        maximum = _positive_int(self.read_backoff_max_ms, "read_backoff_max_ms")
        if maximum < base:
            raise BetfairRequestBudgetError(
                "read_backoff_max_ms must be >= read_backoff_base_ms"
            )


@dataclass(frozen=True, slots=True)
class BetfairRequestIntent:
    """One proposed Betfair request before network dispatch."""

    request_id: str
    operation: BetfairRequestOperation
    priority: BetfairRequestPriority
    market_ids: tuple[str, ...] = ()
    price_data: tuple[str, ...] = ()
    best_prices_depth: int | None = None
    order_projection: str | None = None
    match_projection: str | None = None
    dedupe_key: str | None = None
    reconciliation_for_request_id: str | None = None

    def __post_init__(self) -> None:
        _exact_text(self.request_id, "request_id")
        if type(self.operation) is not BetfairRequestOperation:
            raise BetfairRequestBudgetError(
                "operation must be a BetfairRequestOperation value"
            )
        if type(self.priority) is not BetfairRequestPriority:
            raise BetfairRequestBudgetError(
                "priority must be a BetfairRequestPriority value"
            )
        if type(self.market_ids) is not tuple:
            raise BetfairRequestBudgetError("market_ids must be a tuple")
        normalized_markets = tuple(
            _exact_text(market_id, "market_id") for market_id in self.market_ids
        )
        if len(normalized_markets) != len(set(normalized_markets)):
            raise BetfairRequestBudgetError("market_ids must not contain duplicates")
        if type(self.price_data) is not tuple:
            raise BetfairRequestBudgetError("price_data must be a tuple")
        if self.dedupe_key is not None:
            _exact_text(self.dedupe_key, "dedupe_key")
        if self.reconciliation_for_request_id is not None:
            _exact_text(
                self.reconciliation_for_request_id,
                "reconciliation_for_request_id",
            )

        if self.operation is BetfairRequestOperation.LIST_MARKET_BOOK:
            budget = self._canonical_market_book_budget()
            assert budget is not None
            object.__setattr__(self, "market_ids", budget.market_ids)
            object.__setattr__(self, "price_data", budget.price_data)
            object.__setattr__(self, "best_prices_depth", budget.best_prices_depth)
            self._has_order_aware_projection()
        else:
            if self.market_ids:
                raise BetfairRequestBudgetError(
                    "market_ids are only modeled for listMarketBook"
                )
            if self.price_data:
                raise BetfairRequestBudgetError(
                    "price_data is only valid for listMarketBook"
                )
            if self.best_prices_depth is not None:
                raise BetfairRequestBudgetError(
                    "best_prices_depth is only valid for listMarketBook"
                )
            if self.order_projection is not None or self.match_projection is not None:
                raise BetfairRequestBudgetError(
                    "order/match projection is only valid for listMarketBook"
                )

        if self.reconciliation_for_request_id is not None:
            if self.priority is not BetfairRequestPriority.RECONCILIATION:
                raise BetfairRequestBudgetError(
                    "reconciliation intent must use RECONCILIATION priority"
                )
            if self.operation not in _RECONCILIATION_OPERATIONS:
                raise BetfairRequestBudgetError(
                    "reconciliation intent must use a supported reconciliation read"
                )
        elif self.priority is BetfairRequestPriority.RECONCILIATION:
            raise BetfairRequestBudgetError(
                "RECONCILIATION priority requires reconciliation_for_request_id"
            )

        if self.operation in _MUTATION_OPERATIONS:
            if self.dedupe_key is not None:
                raise BetfairRequestBudgetError(
                    "provider mutations cannot use read coalescing dedupe_key"
                )
            if self.operation is BetfairRequestOperation.CANCEL_ORDERS:
                if self.priority is not BetfairRequestPriority.SAFETY:
                    raise BetfairRequestBudgetError(
                        "cancel_orders must use SAFETY priority"
                    )
            elif self.priority is not BetfairRequestPriority.EXECUTION_MUTATION:
                raise BetfairRequestBudgetError(
                    "place/update/replace must use EXECUTION_MUTATION priority"
                )

    def _canonical_market_book_budget(
        self,
    ) -> _marketbook_budget.MarketBookRequestBudget | None:
        if self.operation is not BetfairRequestOperation.LIST_MARKET_BOOK:
            return None
        try:
            budget = _marketbook_budget.MarketBookRequestBudget(
                market_ids=self.market_ids,
                price_data=self.price_data,
                best_prices_depth=self.best_prices_depth,
                operation="listMarketBook",
            )
            allowed = budget.allowed
        except _marketbook_budget.MarketBookBudgetError as exc:
            raise BetfairRequestBudgetError(
                "listMarketBook request does not have canonical provider budget semantics"
            ) from exc
        if allowed is not True:
            raise BetfairRequestBudgetError(
                "listMarketBook weighted request exceeds 200 points"
            )
        return budget

    def _has_order_aware_projection(self) -> bool:
        for value, field, allowed in (
            (self.order_projection, "order_projection", _ORDER_PROJECTIONS),
            (self.match_projection, "match_projection", _MATCH_PROJECTIONS),
        ):
            if value is None:
                continue
            candidate = _exact_text(value, field)
            if candidate not in allowed:
                raise BetfairRequestBudgetError(
                    f"{field} is not a canonical Betfair projection value"
                )
        return self.order_projection is not None or self.match_projection is not None

    @property
    def is_read(self) -> bool:
        return self.operation in _READ_OPERATIONS

    @property
    def is_mutation(self) -> bool:
        return self.operation in _MUTATION_OPERATIONS

    @property
    def request_pool(self) -> BetfairRequestPool:
        if self.operation is BetfairRequestOperation.LIST_CLEARED_ORDERS:
            return BetfairRequestPool.CLEARED_ORDERS
        if self.operation in {
            BetfairRequestOperation.LIST_CURRENT_ORDERS,
            BetfairRequestOperation.LIST_MARKET_PROFIT_AND_LOSS,
        }:
            return BetfairRequestPool.SHARED_ORDER_READ
        if self.operation is BetfairRequestOperation.LIST_MARKET_BOOK:
            if self._has_order_aware_projection():
                return BetfairRequestPool.SHARED_ORDER_READ
            return BetfairRequestPool.MARKET_DATA
        return BetfairRequestPool.MUTATION

    @property
    def total_market_data_points(self) -> Fraction | None:
        budget = self._canonical_market_book_budget()
        if budget is None:
            return None
        return budget.total_points

    @property
    def has_order_or_match_projection(self) -> bool:
        return self._has_order_aware_projection()


@dataclass(frozen=True, slots=True)
class BetfairMarketBookDispatch:
    market_id: str
    dispatched_monotonic_ns: int

    def __post_init__(self) -> None:
        _exact_text(self.market_id, "market_id")
        _nonnegative_int(self.dispatched_monotonic_ns, "dispatched_monotonic_ns")


@dataclass(frozen=True, slots=True)
class BetfairPoolBackoff:
    pool: BetfairRequestPool
    until_monotonic_ns: int
    failure_count: int

    def __post_init__(self) -> None:
        if type(self.pool) is not BetfairRequestPool:
            raise BetfairRequestBudgetError("pool must be a BetfairRequestPool value")
        _nonnegative_int(self.until_monotonic_ns, "until_monotonic_ns")
        _positive_int(self.failure_count, "failure_count")
        if self.pool is BetfairRequestPool.MUTATION:
            raise BetfairRequestBudgetError(
                "automatic retry backoff is not authority for provider mutations"
            )


@dataclass(frozen=True, slots=True)
class BetfairRequestBudgetState:
    """Process-local rate state plus externally re-resolved UNKNOWN mutation ids."""

    recent_market_book_dispatches: tuple[BetfairMarketBookDispatch, ...] = ()
    in_flight_shared_order_reads: int = 0
    in_flight_cleared_orders: int = 0
    in_flight_market_data: int = 0
    in_flight_mutations: int = 0
    in_flight_request_ids: frozenset[str] = frozenset()
    queued_noncritical_dedupe_keys: frozenset[str] = frozenset()
    unresolved_external_mutation_ids: frozenset[str] = frozenset()
    backoffs: tuple[BetfairPoolBackoff, ...] = ()
    stream_state: BetfairStreamState = BetfairStreamState.UNKNOWN
    cold_start_until_monotonic_ns: int = 0

    def __post_init__(self) -> None:
        if type(self.recent_market_book_dispatches) is not tuple:
            raise BetfairRequestBudgetError(
                "recent_market_book_dispatches must be a tuple"
            )
        for item in self.recent_market_book_dispatches:
            if type(item) is not BetfairMarketBookDispatch:
                raise BetfairRequestBudgetError(
                    "recent_market_book_dispatches must contain exact dispatch values"
                )
        for field in (
            "in_flight_shared_order_reads",
            "in_flight_cleared_orders",
            "in_flight_market_data",
            "in_flight_mutations",
            "cold_start_until_monotonic_ns",
        ):
            _nonnegative_int(getattr(self, field), field)
        if type(self.in_flight_request_ids) is not frozenset:
            raise BetfairRequestBudgetError(
                "in_flight_request_ids must be a frozenset"
            )
        for request_id in self.in_flight_request_ids:
            _exact_text(request_id, "in_flight_request_id")
        if type(self.queued_noncritical_dedupe_keys) is not frozenset:
            raise BetfairRequestBudgetError(
                "queued_noncritical_dedupe_keys must be a frozenset"
            )
        for key in self.queued_noncritical_dedupe_keys:
            _exact_text(key, "queued_noncritical_dedupe_key")
        if type(self.unresolved_external_mutation_ids) is not frozenset:
            raise BetfairRequestBudgetError(
                "unresolved_external_mutation_ids must be a frozenset"
            )
        for request_id in self.unresolved_external_mutation_ids:
            _exact_text(request_id, "unresolved_external_mutation_id")
        if type(self.backoffs) is not tuple:
            raise BetfairRequestBudgetError("backoffs must be a tuple")
        seen_pools: set[BetfairRequestPool] = set()
        for backoff in self.backoffs:
            if type(backoff) is not BetfairPoolBackoff:
                raise BetfairRequestBudgetError(
                    "backoffs must contain exact BetfairPoolBackoff values"
                )
            if backoff.pool in seen_pools:
                raise BetfairRequestBudgetError("backoffs contain duplicate pool")
            seen_pools.add(backoff.pool)
        if type(self.stream_state) is not BetfairStreamState:
            raise BetfairRequestBudgetError(
                "stream_state must be a BetfairStreamState value"
            )

    @classmethod
    def after_restart(
        cls,
        *,
        now_monotonic_ns: int,
        unresolved_external_mutation_ids: frozenset[str] = frozenset(),
        stream_state: BetfairStreamState = BetfairStreamState.UNKNOWN,
    ) -> "BetfairRequestBudgetState":
        """Start conservatively in a new monotonic clock domain.

        Old monotonic timestamps are intentionally not persisted across processes.
        A one-second local cold-start fence prevents a restart burst while unresolved
        external mutations are supplied by their separate durable execution authority.
        """

        now = _nonnegative_int(now_monotonic_ns, "now_monotonic_ns")
        return cls(
            unresolved_external_mutation_ids=unresolved_external_mutation_ids,
            stream_state=stream_state,
            cold_start_until_monotonic_ns=now + _ONE_SECOND_NS,
        )


@dataclass(frozen=True, slots=True)
class BetfairAdmission:
    decision: BetfairAdmissionDecision
    reason: str
    retry_after_monotonic_ns: int | None = None

    def __post_init__(self) -> None:
        if type(self.decision) is not BetfairAdmissionDecision:
            raise BetfairRequestBudgetError(
                "decision must be a BetfairAdmissionDecision value"
            )
        _exact_text(self.reason, "reason")
        if self.retry_after_monotonic_ns is not None:
            _nonnegative_int(
                self.retry_after_monotonic_ns,
                "retry_after_monotonic_ns",
            )


def _pool_in_flight(
    state: BetfairRequestBudgetState,
    pool: BetfairRequestPool,
) -> int:
    return {
        BetfairRequestPool.SHARED_ORDER_READ: state.in_flight_shared_order_reads,
        BetfairRequestPool.CLEARED_ORDERS: state.in_flight_cleared_orders,
        BetfairRequestPool.MARKET_DATA: state.in_flight_market_data,
        BetfairRequestPool.MUTATION: state.in_flight_mutations,
    }[pool]


def _pool_limit(
    policy: BetfairRequestBudgetPolicy,
    pool: BetfairRequestPool,
) -> int:
    return {
        BetfairRequestPool.SHARED_ORDER_READ: policy.shared_order_read_pending_limit,
        BetfairRequestPool.CLEARED_ORDERS: policy.cleared_orders_pending_limit,
        BetfairRequestPool.MARKET_DATA: policy.market_data_pending_limit,
        BetfairRequestPool.MUTATION: policy.mutation_pending_limit,
    }[pool]


def _backoff_for(
    state: BetfairRequestBudgetState,
    pool: BetfairRequestPool,
) -> BetfairPoolBackoff | None:
    for backoff in state.backoffs:
        if backoff.pool is pool:
            return backoff
    return None


def _market_book_retry_after(
    intent: BetfairRequestIntent,
    state: BetfairRequestBudgetState,
    now_monotonic_ns: int,
) -> int | None:
    if intent.operation is not BetfairRequestOperation.LIST_MARKET_BOOK:
        return None
    retry_after: int | None = None
    lower_bound = now_monotonic_ns - _ONE_SECOND_NS
    for market_id in intent.market_ids:
        timestamps = sorted(
            dispatch.dispatched_monotonic_ns
            for dispatch in state.recent_market_book_dispatches
            if dispatch.market_id == market_id
            and dispatch.dispatched_monotonic_ns > lower_bound
            and dispatch.dispatched_monotonic_ns <= now_monotonic_ns
        )
        if len(timestamps) >= _MAX_MARKET_BOOK_PER_MARKET_PER_SECOND:
            candidate = timestamps[-_MAX_MARKET_BOOK_PER_MARKET_PER_SECOND] + _ONE_SECOND_NS
            retry_after = (
                candidate
                if retry_after is None
                else max(retry_after, candidate)
            )
    return retry_after


def admit_betfair_request(
    intent: BetfairRequestIntent,
    *,
    state: BetfairRequestBudgetState,
    policy: BetfairRequestBudgetPolicy,
    now_monotonic_ns: int,
) -> BetfairAdmission:
    """Assess one request against an immutable state snapshot.

    This function is deterministic advisory policy and does not reserve capacity.
    Transport dispatchers that require concurrency/rate enforcement must use
    BetfairRequestBudgetOwner.reserve(), which serializes assessment + reservation.
    """

    if type(intent) is not BetfairRequestIntent:
        raise BetfairRequestBudgetError("intent must be an exact BetfairRequestIntent")
    if type(state) is not BetfairRequestBudgetState:
        raise BetfairRequestBudgetError("state must be an exact BetfairRequestBudgetState")
    if type(policy) is not BetfairRequestBudgetPolicy:
        raise BetfairRequestBudgetError("policy must be an exact BetfairRequestBudgetPolicy")
    now = _nonnegative_int(now_monotonic_ns, "now_monotonic_ns")
    if intent.request_id in state.in_flight_request_ids:
        return BetfairAdmission(
            BetfairAdmissionDecision.THROTTLE,
            "request_id already has an in-flight reservation",
        )

    if any(
        dispatch.dispatched_monotonic_ns > now
        for dispatch in state.recent_market_book_dispatches
    ):
        raise BetfairRequestBudgetError(
            "market-book dispatch history cannot be from the future clock state"
        )

    if (
        state.unresolved_external_mutation_ids
        and intent.operation in _BLOCKED_WHILE_MUTATION_UNKNOWN
    ):
        return BetfairAdmission(
            BetfairAdmissionDecision.BLOCK_RECONCILIATION_REQUIRED,
            "unresolved external mutation requires provider reconciliation before new placement/update/replace",
        )

    if (
        intent.request_pool is BetfairRequestPool.SHARED_ORDER_READ
        and intent.priority is BetfairRequestPriority.RECONCILIATION
        and intent.reconciliation_for_request_id
        not in state.unresolved_external_mutation_ids
    ):
        return BetfairAdmission(
            BetfairAdmissionDecision.THROTTLE,
            "reconciliation reserve requires a currently unresolved external mutation target",
        )

    if (
        intent.dedupe_key is not None
        and intent.priority in {
            BetfairRequestPriority.MONITORING,
            BetfairRequestPriority.BACKGROUND,
        }
        and intent.dedupe_key in state.queued_noncritical_dedupe_keys
    ):
        return BetfairAdmission(
            BetfairAdmissionDecision.COALESCE,
            "identical noncritical read is already queued",
        )

    if (
        state.stream_state is BetfairStreamState.HEALTHY
        and intent.operation is BetfairRequestOperation.LIST_MARKET_BOOK
        and intent.priority in {
            BetfairRequestPriority.MONITORING,
            BetfairRequestPriority.BACKGROUND,
        }
        and not intent.has_order_or_match_projection
    ):
        return BetfairAdmission(
            BetfairAdmissionDecision.DEFER_STREAM_SUFFICIENT,
            "healthy stream cache makes noncritical REST market polling redundant",
        )

    if (
        intent.operation is BetfairRequestOperation.LIST_MARKET_BOOK
        and now < state.cold_start_until_monotonic_ns
    ):
        return BetfairAdmission(
            BetfairAdmissionDecision.THROTTLE,
            "restart cold-start fence prevents immediate market polling burst",
            state.cold_start_until_monotonic_ns,
        )

    pool = intent.request_pool
    backoff = _backoff_for(state, pool)
    if intent.is_read and backoff is not None and now < backoff.until_monotonic_ns:
        return BetfairAdmission(
            BetfairAdmissionDecision.THROTTLE,
            "provider read pool is in bounded backoff",
            backoff.until_monotonic_ns,
        )

    market_retry = _market_book_retry_after(intent, state, now)
    if market_retry is not None and now < market_retry:
        return BetfairAdmission(
            BetfairAdmissionDecision.THROTTLE,
            "listMarketBook per-market rolling rate budget exhausted",
            market_retry,
        )

    in_flight = _pool_in_flight(state, pool)
    limit = _pool_limit(policy, pool)
    effective_limit = limit
    if (
        pool is BetfairRequestPool.SHARED_ORDER_READ
        and intent.priority is not BetfairRequestPriority.RECONCILIATION
    ):
        effective_limit = (
            limit - policy.shared_order_read_reconciliation_reserve
        )

    if in_flight >= effective_limit:
        return BetfairAdmission(
            BetfairAdmissionDecision.THROTTLE,
            "request pool has no admissible concurrency capacity",
        )

    return BetfairAdmission(
        BetfairAdmissionDecision.ADMIT,
        "request fits the bounded provider budget",
    )


def order_betfair_intents(
    intents: Iterable[BetfairRequestIntent],
) -> tuple[BetfairRequestIntent, ...]:
    """Priority-order intents and coalesce identical noncritical reads.

    Input order is not authority. The deterministic request_id tie-break prevents
    arrival-order races from changing routing under provider pressure.
    """

    items = tuple(intents)
    for item in items:
        if type(item) is not BetfairRequestIntent:
            raise BetfairRequestBudgetError(
                "intents must contain exact BetfairRequestIntent values"
            )
    seen_request_ids: set[str] = set()
    for item in items:
        if item.request_id in seen_request_ids:
            raise BetfairRequestBudgetError("duplicate request_id")
        seen_request_ids.add(item.request_id)

    ordered = sorted(items, key=lambda item: (item.priority.rank, item.request_id))
    result: list[BetfairRequestIntent] = []
    seen_noncritical: set[str] = set()
    for item in ordered:
        if (
            item.dedupe_key is not None
            and item.priority in {
                BetfairRequestPriority.MONITORING,
                BetfairRequestPriority.BACKGROUND,
            }
        ):
            if item.dedupe_key in seen_noncritical:
                continue
            seen_noncritical.add(item.dedupe_key)
        result.append(item)
    return tuple(result)


def record_market_book_dispatch(
    state: BetfairRequestBudgetState,
    *,
    intent: BetfairRequestIntent,
    now_monotonic_ns: int,
) -> BetfairRequestBudgetState:
    """Record an already-admitted market-book dispatch for rolling-limit evidence."""

    if type(state) is not BetfairRequestBudgetState:
        raise BetfairRequestBudgetError("state must be an exact BetfairRequestBudgetState")
    if type(intent) is not BetfairRequestIntent:
        raise BetfairRequestBudgetError("intent must be an exact BetfairRequestIntent")
    if intent.operation is not BetfairRequestOperation.LIST_MARKET_BOOK:
        raise BetfairRequestBudgetError(
            "record_market_book_dispatch requires listMarketBook intent"
        )
    now = _nonnegative_int(now_monotonic_ns, "now_monotonic_ns")
    lower_bound = now - _ONE_SECOND_NS
    retained = tuple(
        dispatch
        for dispatch in state.recent_market_book_dispatches
        if dispatch.dispatched_monotonic_ns > lower_bound
        and dispatch.dispatched_monotonic_ns <= now
    )
    appended = tuple(
        BetfairMarketBookDispatch(market_id, now)
        for market_id in intent.market_ids
    )
    return replace(
        state,
        recent_market_book_dispatches=retained + appended,
    )


def _pool_counter_field(pool: BetfairRequestPool) -> str:
    return {
        BetfairRequestPool.SHARED_ORDER_READ: "in_flight_shared_order_reads",
        BetfairRequestPool.CLEARED_ORDERS: "in_flight_cleared_orders",
        BetfairRequestPool.MARKET_DATA: "in_flight_market_data",
        BetfairRequestPool.MUTATION: "in_flight_mutations",
    }[pool]


def _reserve_admitted_request(
    state: BetfairRequestBudgetState,
    *,
    intent: BetfairRequestIntent,
    now_monotonic_ns: int,
) -> BetfairRequestBudgetState:
    if intent.request_id in state.in_flight_request_ids:
        raise BetfairRequestBudgetError("request_id already has an in-flight reservation")
    field = _pool_counter_field(intent.request_pool)
    reserved = replace(
        state,
        **{
            field: getattr(state, field) + 1,
            "in_flight_request_ids": state.in_flight_request_ids | {intent.request_id},
        },
    )
    if intent.operation is BetfairRequestOperation.LIST_MARKET_BOOK:
        reserved = record_market_book_dispatch(
            reserved,
            intent=intent,
            now_monotonic_ns=now_monotonic_ns,
        )
    return reserved


def release_betfair_request(
    state: BetfairRequestBudgetState,
    *,
    intent: BetfairRequestIntent,
) -> BetfairRequestBudgetState:
    """Release one exact in-flight reservation after transport completion/abort."""

    if type(state) is not BetfairRequestBudgetState:
        raise BetfairRequestBudgetError("state must be an exact BetfairRequestBudgetState")
    if type(intent) is not BetfairRequestIntent:
        raise BetfairRequestBudgetError("intent must be an exact BetfairRequestIntent")
    if intent.request_id not in state.in_flight_request_ids:
        raise BetfairRequestBudgetError("request_id has no in-flight reservation")
    field = _pool_counter_field(intent.request_pool)
    current = getattr(state, field)
    if current <= 0:
        raise BetfairRequestBudgetError("in-flight pool counter cannot underflow")
    return replace(
        state,
        **{
            field: current - 1,
            "in_flight_request_ids": state.in_flight_request_ids - {intent.request_id},
        },
    )


class BetfairRequestBudgetOwner:
    """Process-local serialized owner of provider-pressure reservation state.

    The owner closes check-then-act races inside one process. It intentionally does
    not claim cross-process/distributed coordination authority; callers must route a
    provider account through one owner (or an external serialized equivalent).
    """

    def __init__(self, state: BetfairRequestBudgetState | None = None) -> None:
        if state is None:
            state = BetfairRequestBudgetState()
        if type(state) is not BetfairRequestBudgetState:
            raise BetfairRequestBudgetError(
                "state must be an exact BetfairRequestBudgetState"
            )
        self._state = state
        self._lock = Lock()

    def snapshot(self) -> BetfairRequestBudgetState:
        with self._lock:
            return self._state

    def reserve(
        self,
        intent: BetfairRequestIntent,
        *,
        policy: BetfairRequestBudgetPolicy,
        now_monotonic_ns: int,
    ) -> BetfairAdmission:
        """Atomically assess and reserve one request before provider dispatch."""

        with self._lock:
            admission = admit_betfair_request(
                intent,
                state=self._state,
                policy=policy,
                now_monotonic_ns=now_monotonic_ns,
            )
            if admission.decision is BetfairAdmissionDecision.ADMIT:
                self._state = _reserve_admitted_request(
                    self._state,
                    intent=intent,
                    now_monotonic_ns=now_monotonic_ns,
                )
            return admission

    def release(self, intent: BetfairRequestIntent) -> BetfairRequestBudgetState:
        """Release one reservation and return the new immutable state snapshot."""

        with self._lock:
            self._state = release_betfair_request(self._state, intent=intent)
            return self._state


def record_read_backpressure(
    state: BetfairRequestBudgetState,
    *,
    pool: BetfairRequestPool,
    policy: BetfairRequestBudgetPolicy,
    now_monotonic_ns: int,
) -> BetfairRequestBudgetState:
    """Advance bounded exponential backoff for an idempotent read pool.

    Mutation pools are intentionally rejected: provider-write ambiguity must be
    reconciled by the execution authority, not retried by this rate-budget contract.
    """

    if type(state) is not BetfairRequestBudgetState:
        raise BetfairRequestBudgetError("state must be an exact BetfairRequestBudgetState")
    if type(policy) is not BetfairRequestBudgetPolicy:
        raise BetfairRequestBudgetError("policy must be an exact BetfairRequestBudgetPolicy")
    if type(pool) is not BetfairRequestPool:
        raise BetfairRequestBudgetError("pool must be a BetfairRequestPool value")
    if pool is BetfairRequestPool.MUTATION:
        raise BetfairRequestBudgetError(
            "provider mutations cannot acquire automatic retry backoff"
        )
    now = _nonnegative_int(now_monotonic_ns, "now_monotonic_ns")
    current = _backoff_for(state, pool)
    failure_count = 1 if current is None else current.failure_count + 1
    multiplier = 1 << min(failure_count - 1, 30)
    delay_ms = min(
        policy.read_backoff_base_ms * multiplier,
        policy.read_backoff_max_ms,
    )
    replacement = BetfairPoolBackoff(
        pool=pool,
        until_monotonic_ns=now + delay_ms * 1_000_000,
        failure_count=failure_count,
    )
    backoffs = tuple(item for item in state.backoffs if item.pool is not pool)
    return replace(
        state,
        backoffs=tuple(
            sorted(backoffs + (replacement,), key=lambda item: item.pool.value)
        ),
    )


def clear_read_backpressure(
    state: BetfairRequestBudgetState,
    *,
    pool: BetfairRequestPool,
) -> BetfairRequestBudgetState:
    """Clear one read-pool backoff after a confirmed successful provider read."""

    if type(state) is not BetfairRequestBudgetState:
        raise BetfairRequestBudgetError(
            "state must be an exact BetfairRequestBudgetState"
        )
    if type(pool) is not BetfairRequestPool:
        raise BetfairRequestBudgetError(
            "pool must be a BetfairRequestPool value"
        )
    if pool is BetfairRequestPool.MUTATION:
        raise BetfairRequestBudgetError(
            "mutation pool has no automatic read backoff to clear"
        )
    return replace(
        state,
        backoffs=tuple(item for item in state.backoffs if item.pool is not pool),
    )
