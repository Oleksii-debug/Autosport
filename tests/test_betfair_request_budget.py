import pytest

from autosport.betfair_request_budget import (
    BetfairAdmissionDecision,
    BetfairRequestBudgetError,
    BetfairRequestBudgetPolicy,
    BetfairRequestBudgetState,
    BetfairRequestIntent,
    BetfairRequestOperation,
    BetfairRequestPool,
    BetfairRequestPriority,
    BetfairStreamState,
    admit_betfair_request,
    clear_read_backpressure,
    order_betfair_intents,
    record_market_book_dispatch,
    record_read_backpressure,
)


def _policy() -> BetfairRequestBudgetPolicy:
    return BetfairRequestBudgetPolicy(
        shared_order_read_pending_limit=4,
        shared_order_read_reconciliation_reserve=1,
        cleared_orders_pending_limit=2,
        market_data_pending_limit=3,
        mutation_pending_limit=1,
        read_backoff_base_ms=100,
        read_backoff_max_ms=800,
    )


def _market(
    request_id: str,
    *,
    market: str = "1.234",
    weight: int = 1,
    priority: BetfairRequestPriority = BetfairRequestPriority.MONITORING,
    projection: bool = False,
    dedupe_key: str | None = None,
) -> BetfairRequestIntent:
    return BetfairRequestIntent(
        request_id=request_id,
        operation=BetfairRequestOperation.LIST_MARKET_BOOK,
        priority=priority,
        market_ids=(market,),
        market_data_weight_per_market=weight,
        uses_order_projection=projection,
        dedupe_key=dedupe_key,
    )


def _reconcile(
    request_id: str = "reconcile-1",
    *,
    target_request_id: str = "write-timeout-1",
) -> BetfairRequestIntent:
    return BetfairRequestIntent(
        request_id=request_id,
        operation=BetfairRequestOperation.LIST_CURRENT_ORDERS,
        priority=BetfairRequestPriority.RECONCILIATION,
        reconciliation_for_request_id=target_request_id,
    )


def _place(request_id: str = "place-2") -> BetfairRequestIntent:
    return BetfairRequestIntent(
        request_id=request_id,
        operation=BetfairRequestOperation.PLACE_ORDERS,
        priority=BetfairRequestPriority.EXECUTION_MUTATION,
    )


def test_weighted_market_request_over_200_points_fails_before_dispatch() -> None:
    with pytest.raises(
        BetfairRequestBudgetError,
        match="exceeds 200 points",
    ):
        BetfairRequestIntent(
            request_id="market-heavy",
            operation=BetfairRequestOperation.LIST_MARKET_BOOK,
            priority=BetfairRequestPriority.EXECUTION_READ,
            market_ids=("1", "2", "3"),
            market_data_weight_per_market=67,
        )


def test_sixth_same_market_dispatch_in_rolling_second_is_throttled() -> None:
    state = BetfairRequestBudgetState()
    intent = _market("poll-1")
    start = 10_000_000_000

    for offset in range(5):
        now = start + offset * 100_000_000
        assert (
            admit_betfair_request(
                intent,
                state=state,
                policy=_policy(),
                now_monotonic_ns=now,
            ).decision
            is BetfairAdmissionDecision.ADMIT
        )
        state = record_market_book_dispatch(
            state,
            intent=intent,
            now_monotonic_ns=now,
        )

    sixth = admit_betfair_request(
        intent,
        state=state,
        policy=_policy(),
        now_monotonic_ns=start + 500_000_000,
    )
    assert sixth.decision is BetfairAdmissionDecision.THROTTLE
    assert sixth.retry_after_monotonic_ns == start + 1_000_000_000


def test_one_second_old_market_dispatch_no_longer_consumes_rate_budget() -> None:
    state = BetfairRequestBudgetState()
    intent = _market("poll-1")
    start = 10_000_000_000

    for offset in range(5):
        state = record_market_book_dispatch(
            state,
            intent=intent,
            now_monotonic_ns=start + offset,
        )

    result = admit_betfair_request(
        intent,
        state=state,
        policy=_policy(),
        now_monotonic_ns=start + 1_000_000_000,
    )
    assert result.decision is BetfairAdmissionDecision.ADMIT


def test_reconciliation_reserve_cannot_be_consumed_by_monitoring_projection_reads() -> None:
    state = BetfairRequestBudgetState(
        in_flight_shared_order_reads=3,
        unresolved_external_mutation_ids=frozenset({"write-timeout-1"}),
    )
    monitoring = _market(
        "monitoring-projection",
        priority=BetfairRequestPriority.MONITORING,
        projection=True,
    )

    blocked = admit_betfair_request(
        monitoring,
        state=state,
        policy=_policy(),
        now_monotonic_ns=0,
    )
    reconciliation = admit_betfair_request(
        _reconcile(),
        state=state,
        policy=_policy(),
        now_monotonic_ns=0,
    )

    assert blocked.decision is BetfairAdmissionDecision.THROTTLE
    assert reconciliation.decision is BetfairAdmissionDecision.ADMIT


def test_invented_reconciliation_target_cannot_steal_reserved_shared_read_capacity() -> None:
    state = BetfairRequestBudgetState(
        in_flight_shared_order_reads=3,
        unresolved_external_mutation_ids=frozenset(),
    )

    result = admit_betfair_request(
        _reconcile(target_request_id="invented-write"),
        state=state,
        policy=_policy(),
        now_monotonic_ns=0,
    )

    assert result.decision is BetfairAdmissionDecision.THROTTLE
    assert "currently unresolved" in result.reason


def test_resolved_reconciliation_target_cannot_reclaim_reserved_shared_read_capacity() -> None:
    intent = _reconcile(target_request_id="write-timeout-1")
    unresolved = BetfairRequestBudgetState(
        in_flight_shared_order_reads=3,
        unresolved_external_mutation_ids=frozenset({"write-timeout-1"}),
    )
    resolved = BetfairRequestBudgetState(
        in_flight_shared_order_reads=3,
        unresolved_external_mutation_ids=frozenset(),
    )

    assert (
        admit_betfair_request(
            intent,
            state=unresolved,
            policy=_policy(),
            now_monotonic_ns=0,
        ).decision
        is BetfairAdmissionDecision.ADMIT
    )
    assert (
        admit_betfair_request(
            intent,
            state=resolved,
            policy=_policy(),
            now_monotonic_ns=0,
        ).decision
        is BetfairAdmissionDecision.THROTTLE
    )


def test_list_cleared_orders_uses_separate_pool_when_shared_reads_are_full() -> None:
    state = BetfairRequestBudgetState(
        in_flight_shared_order_reads=4,
    )
    cleared = BetfairRequestIntent(
        request_id="settlement-reconcile",
        operation=BetfairRequestOperation.LIST_CLEARED_ORDERS,
        priority=BetfairRequestPriority.RECONCILIATION,
        reconciliation_for_request_id="settlement-unknown-1",
    )

    result = admit_betfair_request(
        cleared,
        state=state,
        policy=_policy(),
        now_monotonic_ns=0,
    )
    assert cleared.request_pool is BetfairRequestPool.CLEARED_ORDERS
    assert result.decision is BetfairAdmissionDecision.ADMIT


def test_unknown_external_mutation_blocks_new_place_until_reconciliation() -> None:
    state = BetfairRequestBudgetState(
        unresolved_external_mutation_ids=frozenset({"write-timeout-1"}),
    )

    placement = admit_betfair_request(
        _place(),
        state=state,
        policy=_policy(),
        now_monotonic_ns=0,
    )
    reconciliation = admit_betfair_request(
        _reconcile(),
        state=state,
        policy=_policy(),
        now_monotonic_ns=0,
    )

    assert (
        placement.decision
        is BetfairAdmissionDecision.BLOCK_RECONCILIATION_REQUIRED
    )
    assert reconciliation.decision is BetfairAdmissionDecision.ADMIT


def test_unknown_external_mutation_blocks_replace_and_update_but_not_safety_cancel() -> None:
    state = BetfairRequestBudgetState(
        unresolved_external_mutation_ids=frozenset({"write-timeout-1"}),
    )
    for operation in (
        BetfairRequestOperation.UPDATE_ORDERS,
        BetfairRequestOperation.REPLACE_ORDERS,
    ):
        intent = BetfairRequestIntent(
            request_id=f"next-{operation.value}",
            operation=operation,
            priority=BetfairRequestPriority.EXECUTION_MUTATION,
        )
        assert (
            admit_betfair_request(
                intent,
                state=state,
                policy=_policy(),
                now_monotonic_ns=0,
            ).decision
            is BetfairAdmissionDecision.BLOCK_RECONCILIATION_REQUIRED
        )

    cancel = BetfairRequestIntent(
        request_id="safety-cancel",
        operation=BetfairRequestOperation.CANCEL_ORDERS,
        priority=BetfairRequestPriority.SAFETY,
    )
    assert (
        admit_betfair_request(
            cancel,
            state=state,
            policy=_policy(),
            now_monotonic_ns=0,
        ).decision
        is BetfairAdmissionDecision.ADMIT
    )


def test_mutation_priority_contract_cannot_mislabel_cancel_or_place() -> None:
    with pytest.raises(BetfairRequestBudgetError, match="SAFETY"):
        BetfairRequestIntent(
            request_id="bad-cancel",
            operation=BetfairRequestOperation.CANCEL_ORDERS,
            priority=BetfairRequestPriority.EXECUTION_MUTATION,
        )
    with pytest.raises(BetfairRequestBudgetError, match="EXECUTION_MUTATION"):
        BetfairRequestIntent(
            request_id="bad-place",
            operation=BetfairRequestOperation.PLACE_ORDERS,
            priority=BetfairRequestPriority.SAFETY,
        )


def test_priority_order_puts_reconciliation_before_background_and_new_mutation() -> None:
    background = BetfairRequestIntent(
        request_id="background",
        operation=BetfairRequestOperation.LIST_CLEARED_ORDERS,
        priority=BetfairRequestPriority.BACKGROUND,
    )
    placement = _place()
    reconciliation = _reconcile()

    ordered = order_betfair_intents((background, placement, reconciliation))

    assert [item.request_id for item in ordered] == [
        reconciliation.request_id,
        placement.request_id,
        background.request_id,
    ]


def test_identical_noncritical_reads_are_coalesced_but_reconciliation_is_not() -> None:
    first = _market("monitor-a", dedupe_key="market:1.234")
    second = _market("monitor-b", dedupe_key="market:1.234")
    recon_a = _reconcile("recon-a")
    recon_b = BetfairRequestIntent(
        request_id="recon-b",
        operation=BetfairRequestOperation.LIST_CURRENT_ORDERS,
        priority=BetfairRequestPriority.RECONCILIATION,
        reconciliation_for_request_id="write-timeout-2",
    )

    ordered = order_betfair_intents((second, recon_b, first, recon_a))
    ids = [item.request_id for item in ordered]

    assert ids[:2] == ["recon-a", "recon-b"]
    assert ids.count("monitor-a") == 1
    assert "monitor-b" not in ids


def test_already_queued_noncritical_read_is_coalesced_at_admission() -> None:
    state = BetfairRequestBudgetState(
        queued_noncritical_dedupe_keys=frozenset({"market:1.234"}),
    )

    result = admit_betfair_request(
        _market("monitor", dedupe_key="market:1.234"),
        state=state,
        policy=_policy(),
        now_monotonic_ns=0,
    )
    assert result.decision is BetfairAdmissionDecision.COALESCE


def test_healthy_stream_defers_redundant_monitoring_rest_but_not_execution_read() -> None:
    state = BetfairRequestBudgetState(stream_state=BetfairStreamState.HEALTHY)

    monitoring = admit_betfair_request(
        _market("monitor"),
        state=state,
        policy=_policy(),
        now_monotonic_ns=0,
    )
    execution = admit_betfair_request(
        _market(
            "execution-read",
            priority=BetfairRequestPriority.EXECUTION_READ,
        ),
        state=state,
        policy=_policy(),
        now_monotonic_ns=0,
    )

    assert (
        monitoring.decision
        is BetfairAdmissionDecision.DEFER_STREAM_SUFFICIENT
    )
    assert execution.decision is BetfairAdmissionDecision.ADMIT


def test_degraded_stream_allows_bounded_rest_fallback() -> None:
    state = BetfairRequestBudgetState(stream_state=BetfairStreamState.DEGRADED)
    result = admit_betfair_request(
        _market("fallback"),
        state=state,
        policy=_policy(),
        now_monotonic_ns=0,
    )
    assert result.decision is BetfairAdmissionDecision.ADMIT


def test_restart_enters_one_second_market_polling_cold_start_fence() -> None:
    state = BetfairRequestBudgetState.after_restart(
        now_monotonic_ns=5_000,
        unresolved_external_mutation_ids=frozenset({"write-timeout-1"}),
    )

    market = admit_betfair_request(
        _market("after-restart"),
        state=state,
        policy=_policy(),
        now_monotonic_ns=5_000,
    )
    placement = admit_betfair_request(
        _place(),
        state=state,
        policy=_policy(),
        now_monotonic_ns=5_000,
    )
    reconciliation = admit_betfair_request(
        _reconcile(),
        state=state,
        policy=_policy(),
        now_monotonic_ns=5_000,
    )

    assert market.decision is BetfairAdmissionDecision.THROTTLE
    assert market.retry_after_monotonic_ns == 1_000_005_000
    assert (
        placement.decision
        is BetfairAdmissionDecision.BLOCK_RECONCILIATION_REQUIRED
    )
    assert reconciliation.decision is BetfairAdmissionDecision.ADMIT


def test_read_backpressure_is_exponential_bounded_and_does_not_authorize_write_retry() -> None:
    state = BetfairRequestBudgetState()
    policy = _policy()
    now = 1_000_000_000

    expected_ms = (100, 200, 400, 800, 800)
    for failure, expected in enumerate(expected_ms, start=1):
        state = record_read_backpressure(
            state,
            pool=BetfairRequestPool.MARKET_DATA,
            policy=policy,
            now_monotonic_ns=now,
        )
        backoff = next(
            item
            for item in state.backoffs
            if item.pool is BetfairRequestPool.MARKET_DATA
        )
        assert backoff.failure_count == failure
        assert backoff.until_monotonic_ns == now + expected * 1_000_000
        now = backoff.until_monotonic_ns

    with pytest.raises(
        BetfairRequestBudgetError,
        match="mutations cannot acquire automatic retry backoff",
    ):
        record_read_backpressure(
            state,
            pool=BetfairRequestPool.MUTATION,
            policy=policy,
            now_monotonic_ns=now,
        )


def test_active_read_backoff_throttles_without_fanout_retry() -> None:
    policy = _policy()
    state = record_read_backpressure(
        BetfairRequestBudgetState(),
        pool=BetfairRequestPool.MARKET_DATA,
        policy=policy,
        now_monotonic_ns=1_000_000_000,
    )
    result = admit_betfair_request(
        _market("backoff-poll"),
        state=state,
        policy=policy,
        now_monotonic_ns=1_050_000_000,
    )

    assert result.decision is BetfairAdmissionDecision.THROTTLE
    assert result.retry_after_monotonic_ns == 1_100_000_000


def test_request_ids_are_unique_and_input_order_is_not_priority_authority() -> None:
    one = _market("same-id")
    two = BetfairRequestIntent(
        request_id="same-id",
        operation=BetfairRequestOperation.LIST_CLEARED_ORDERS,
        priority=BetfairRequestPriority.BACKGROUND,
    )

    with pytest.raises(BetfairRequestBudgetError, match="duplicate request_id"):
        order_betfair_intents((one, two))


def test_bool_aliases_do_not_pass_integer_policy_or_clock_fields() -> None:
    with pytest.raises(BetfairRequestBudgetError, match="exact integer"):
        BetfairRequestBudgetPolicy(
            shared_order_read_pending_limit=True,
            shared_order_read_reconciliation_reserve=1,
            cleared_orders_pending_limit=2,
            market_data_pending_limit=3,
            mutation_pending_limit=1,
            read_backoff_base_ms=100,
            read_backoff_max_ms=800,
        )
    with pytest.raises(BetfairRequestBudgetError, match="exact integer"):
        admit_betfair_request(
            _market("clock"),
            state=BetfairRequestBudgetState(),
            policy=_policy(),
            now_monotonic_ns=True,
        )


def test_future_market_dispatch_history_fails_closed_instead_of_bypassing_limit() -> None:
    intent = _market("future-history")
    state = record_market_book_dispatch(
        BetfairRequestBudgetState(),
        intent=intent,
        now_monotonic_ns=2_000,
    )

    with pytest.raises(
        BetfairRequestBudgetError,
        match="cannot be from the future",
    ):
        admit_betfair_request(
            intent,
            state=state,
            policy=_policy(),
            now_monotonic_ns=1_999,
        )


def test_successful_read_clears_only_its_pool_backoff() -> None:
    policy = _policy()
    state = record_read_backpressure(
        BetfairRequestBudgetState(),
        pool=BetfairRequestPool.MARKET_DATA,
        policy=policy,
        now_monotonic_ns=1_000,
    )
    state = record_read_backpressure(
        state,
        pool=BetfairRequestPool.CLEARED_ORDERS,
        policy=policy,
        now_monotonic_ns=1_000,
    )

    cleared = clear_read_backpressure(
        state,
        pool=BetfairRequestPool.MARKET_DATA,
    )

    assert all(
        item.pool is not BetfairRequestPool.MARKET_DATA
        for item in cleared.backoffs
    )
    assert any(
        item.pool is BetfairRequestPool.CLEARED_ORDERS
        for item in cleared.backoffs
    )
