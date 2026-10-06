from __future__ import annotations

import pytest

from autosport.betfair_request_budget import (
    BetfairAdmissionDecision,
    BetfairRequestBudgetError,
    BetfairRequestBudgetOwner,
    BetfairRequestBudgetPolicy,
    BetfairRequestBudgetState,
    BetfairRequestIntent,
    BetfairRequestOperation,
    BetfairRequestPriority,
    market_book_intent_from_rpc_params,
    release_betfair_request,
)


def _policy() -> BetfairRequestBudgetPolicy:
    return BetfairRequestBudgetPolicy(
        shared_order_read_pending_limit=2,
        shared_order_read_reconciliation_reserve=1,
        cleared_orders_pending_limit=2,
        market_data_pending_limit=1,
        mutation_pending_limit=1,
        read_backoff_base_ms=100,
        read_backoff_max_ms=800,
    )


def _market(request_id: str, market_id: str) -> BetfairRequestIntent:
    return market_book_intent_from_rpc_params(
        request_id,
        BetfairRequestPriority.EXECUTION_READ,
        params={"marketIds": [market_id]},
    )


def _ordinary_current_orders(request_id: str) -> BetfairRequestIntent:
    return BetfairRequestIntent(
        request_id=request_id,
        operation=BetfairRequestOperation.LIST_CURRENT_ORDERS,
        priority=BetfairRequestPriority.EXECUTION_READ,
    )


def test_stale_state_cannot_double_admit_last_market_data_slot() -> None:
    """Admission must reserve capacity atomically, not only inspect a stale snapshot."""

    state = BetfairRequestBudgetState(in_flight_market_data=0)
    policy = _policy()

    owner = BetfairRequestBudgetOwner(state)
    first = owner.reserve(
        _market("market-a", "1.100"),
        policy=policy,
        now_monotonic_ns=1,
    )
    second = owner.reserve(
        _market("market-b", "1.200"),
        policy=policy,
        now_monotonic_ns=1,
    )

    admitted = sum(
        decision.decision is BetfairAdmissionDecision.ADMIT
        for decision in (first, second)
    )
    assert admitted == 1, (
        "two dispatchers that observe the same last-capacity state must not both "
        "receive ADMIT before either has reserved the provider slot"
    )


def test_stale_state_cannot_double_admit_reserved_shared_order_capacity() -> None:
    """The reconciliation reserve cannot be protected by a check-then-act race."""

    state = BetfairRequestBudgetState(in_flight_shared_order_reads=0)
    policy = _policy()

    owner = BetfairRequestBudgetOwner(state)
    first = owner.reserve(
        _ordinary_current_orders("orders-a"),
        policy=policy,
        now_monotonic_ns=1,
    )
    second = owner.reserve(
        _ordinary_current_orders("orders-b"),
        policy=policy,
        now_monotonic_ns=1,
    )

    admitted = sum(
        decision.decision is BetfairAdmissionDecision.ADMIT
        for decision in (first, second)
    )
    assert admitted == 1, (
        "ordinary shared-order reads must not both consume the single non-reserved "
        "slot when they race from the same immutable state"
    )


def test_reservation_release_is_exact_and_cannot_underflow() -> None:
    policy = _policy()
    intent = _market("market-release", "1.300")
    owner = BetfairRequestBudgetOwner()

    admitted = owner.reserve(intent, policy=policy, now_monotonic_ns=10)
    assert admitted.decision is BetfairAdmissionDecision.ADMIT
    reserved = owner.snapshot()
    assert reserved.in_flight_market_data == 1
    assert reserved.in_flight_request_ids == frozenset({"market-release"})
    assert len(reserved.recent_market_book_dispatches) == 1

    released = owner.release(intent)
    assert released.in_flight_market_data == 0
    assert released.in_flight_request_ids == frozenset()

    with pytest.raises(BetfairRequestBudgetError, match="no in-flight reservation"):
        release_betfair_request(released, intent=intent)


def test_owner_reservation_counts_toward_rolling_market_rate_limit() -> None:
    policy = BetfairRequestBudgetPolicy(
        shared_order_read_pending_limit=2,
        shared_order_read_reconciliation_reserve=1,
        cleared_orders_pending_limit=2,
        market_data_pending_limit=2,
        mutation_pending_limit=1,
        read_backoff_base_ms=100,
        read_backoff_max_ms=800,
    )
    owner = BetfairRequestBudgetOwner()
    start = 1_000_000_000

    for offset in range(5):
        intent = _market(f"rate-{offset}", "1.400")
        result = owner.reserve(
            intent,
            policy=policy,
            now_monotonic_ns=start + offset,
        )
        assert result.decision is BetfairAdmissionDecision.ADMIT
        owner.release(intent)

    sixth = owner.reserve(
        _market("rate-sixth", "1.400"),
        policy=policy,
        now_monotonic_ns=start + 10,
    )
    assert sixth.decision is BetfairAdmissionDecision.THROTTLE


def test_owner_keeps_mutation_rate_history_after_concurrency_release() -> None:
    policy = _policy()
    first = BetfairRequestIntent(
        request_id="place-1000",
        operation=BetfairRequestOperation.PLACE_ORDERS,
        priority=BetfairRequestPriority.EXECUTION_MUTATION,
        mutation_instruction_count=1000,
    )
    second = BetfairRequestIntent(
        request_id="cancel-1",
        operation=BetfairRequestOperation.CANCEL_ORDERS,
        priority=BetfairRequestPriority.SAFETY,
        mutation_instruction_count=1,
    )
    owner = BetfairRequestBudgetOwner()

    admitted = owner.reserve(first, policy=policy, now_monotonic_ns=100)
    assert admitted.decision is BetfairAdmissionDecision.ADMIT
    owner.release(first)
    assert owner.snapshot().in_flight_mutations == 0

    throttled = owner.reserve(second, policy=policy, now_monotonic_ns=101)
    assert throttled.decision is BetfairAdmissionDecision.THROTTLE
    assert throttled.retry_after_monotonic_ns == 1_000_000_100
