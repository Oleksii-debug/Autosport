from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest

from autosport.betfair_account_readonly import (
    BETTING_JSON_RPC_ENDPOINT,
    BetfairReadOnlyClient,
    BetfairReadOnlyError,
    BetfairSessionCredentials,
)
from autosport.betfair_marketbook_batch_completeness import BatchReceiptStatus
from autosport.betfair_marketbook_rate_gate import BetfairMarketBookPerMarketRateGate
from autosport.betfair_marketbook_projection_concurrency import (
    BetfairMarketBookProjectionConcurrencyGate,
)
from autosport.betfair_marketbook_batch_plan import MarketBookReadPlan
from autosport.betfair_marketbook_attempt_history import (
    MarketBookAttemptHistory,
    MarketBookAttemptOutcome,
)
from autosport.betfair_marketbook_batch_transport import (
    MarketBookBatchAdmissionError,
    MarketBookBatchAttemptExecution,
    MarketBookBatchTransportError,
    MarketBookBatchTransportResult,
    append_market_book_transport_attempt,
    execute_market_book_batch_attempt,
    read_market_book_batch,
)


NOW = datetime(2026, 10, 6, 17, 0, tzinfo=timezone.utc)


class FakeTransport:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.calls: list[dict[str, object]] = []

    def post(self, url, *, headers, body, timeout_seconds):
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout_seconds": timeout_seconds,
            }
        )
        return self.payload


def _payload(market_ids, *, request_id: int = 1) -> bytes:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "result": [{"marketId": market_id} for market_id in market_ids],
            "id": request_id,
        },
        separators=(",", ":"),
    ).encode("utf-8")


def _client(payload: bytes) -> tuple[BetfairReadOnlyClient, FakeTransport]:
    transport = FakeTransport(payload)
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-secret", "session-secret"),
        transport=transport,
        clock=lambda: NOW,
        venue_id="betfair-global",
        account_id="configured-account",
    )
    return client, transport


def _plan(market_ids=("1.001", "1.002"), **kwargs) -> MarketBookReadPlan:
    return MarketBookReadPlan(
        market_ids=tuple(market_ids),
        market_status="OPEN",
        **kwargs,
    )


def _gates():
    return (
        BetfairMarketBookPerMarketRateGate(),
        BetfairMarketBookProjectionConcurrencyGate(),
    )


def _read(client, plan, *, batch_id, request_id="request-1", scheduled_at=NOW):
    rate_gate, concurrency_gate = _gates()
    return read_market_book_batch(
        client,
        plan,
        batch_id=batch_id,
        request_id=request_id,
        scheduled_at=scheduled_at,
        rate_gate=rate_gate,
        concurrency_gate=concurrency_gate,
    )


def test_exact_batch_read_maps_full_canonical_contract_to_wire_and_origin_binds_result():
    plan = _plan(
        price_data=("EX_BEST_OFFERS",),
        best_prices_depth=2,
        virtualise=False,
        rollover_stakes=False,
        order_projection="EXECUTABLE",
        match_projection="ROLLED_UP_BY_AVG_PRICE",
        include_overall_position=True,
        partition_matched_by_strategy_ref=False,
        customer_strategy_refs=("alpha",),
        matched_since="2026-10-06T16:00:00+00:00",
        bet_ids=("bet-1",),
        currency_code="GBP",
        locale="en",
        rollup_model="STAKE",
        rollup_limit=5,
    )
    batch = plan.batches[0]
    client, transport = _client(_payload(batch.market_ids))

    result = _read(client, plan, batch_id=batch.batch_id)

    result.assert_issued()
    with pytest.raises(
        MarketBookBatchTransportError,
        match="lacks canonical network origin",
    ):
        result.assert_canonical_network_origin()
    assert result.plan_id == plan.plan_id
    assert result.request_contract_id == plan.request_contract_id
    assert result.batch_id == batch.batch_id
    assert result.request_budget_evidence_id == batch.budget_evidence_id
    assert result.receipt.status is BatchReceiptStatus.EXACT_RESPONSE
    assert result.structural_exact_response is True
    assert result.canonical_network_origin is False
    assert result.evidence_payload["provider_observation_authenticated"] is False
    assert result.evidence_payload["provider_freshness_proven"] is False
    assert result.evidence_payload["provider_write_authorized"] is False
    assert result.evidence_payload["execution_authorized"] is False

    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["url"] == BETTING_JSON_RPC_ENDPOINT
    request = json.loads(call["body"])
    assert request["method"] == "SportsAPING/v1.0/listMarketBook"
    assert request["params"] == {
        "marketIds": ["1.001", "1.002"],
        "priceProjection": {
            "priceData": ["EX_BEST_OFFERS"],
            "virtualise": False,
            "rolloverStakes": False,
            "exBestOffersOverrides": {
                "bestPricesDepth": 2,
                "rollupModel": "STAKE",
                "rollupLimit": 5,
            },
        },
        "orderProjection": "EXECUTABLE",
        "matchProjection": "ROLLED_UP_BY_AVG_PRICE",
        "includeOverallPosition": True,
        "partitionMatchedByStrategyRef": False,
        "customerStrategyRefs": ["alpha"],
        "matchedSince": "2026-10-06T16:00:00+00:00",
        "betIds": ["bet-1"],
        "currencyCode": "GBP",
        "locale": "en",
    }
    assert b"app-secret" not in call["body"]
    assert b"session-secret" not in call["body"]
    assert "app-secret" not in json.dumps(result.evidence_payload)
    assert "session-secret" not in json.dumps(result.evidence_payload)


def test_incomplete_provider_response_remains_structurally_incomplete():
    plan = _plan()
    batch = plan.batches[0]
    client, _ = _client(_payload(("1.001",)))

    result = _read(client, plan, batch_id=batch.batch_id)

    result.assert_issued()
    assert result.receipt.status is BatchReceiptStatus.INCOMPLETE_RESPONSE
    assert result.receipt.missing_market_ids == ("1.002",)
    assert result.structural_exact_response is False


def test_forged_copy_cannot_inherit_transport_issuance():
    plan = _plan()
    batch = plan.batches[0]
    client, _ = _client(_payload(batch.market_ids))
    issued = _read(client, plan, batch_id=batch.batch_id)

    forged = replace(issued)

    with pytest.raises(
        MarketBookBatchTransportError,
        match="not issued by canonical transport",
    ):
        forged.assert_issued()


def test_plan_use_time_mutation_invalidates_old_batch_before_transport():
    plan = _plan(
        market_ids=("1.001", "1.002", "1.003"),
        price_data=("EX_BEST_OFFERS",),
    )
    old_batch_id = plan.batches[0].batch_id
    client, transport = _client(_payload(plan.batches[0].market_ids))
    object.__setattr__(plan, "best_prices_depth", 10)

    with pytest.raises(
        MarketBookBatchTransportError,
        match="batch_id must identify exactly one current canonical plan batch",
    ):
        _read(client, plan, batch_id=old_batch_id)

    assert transport.calls == []


def test_plan_mutation_during_physical_post_cannot_mint_bound_result():
    plan = _plan(
        price_data=("EX_BEST_OFFERS",),
        best_prices_depth=1,
    )
    batch = plan.batches[0]

    class MutatingPlanTransport:
        def __init__(self) -> None:
            self.calls = 0

        def post(self, url, *, headers, body, timeout_seconds):
            self.calls += 1
            object.__setattr__(plan, "best_prices_depth", 2)
            return _payload(batch.market_ids)

    transport = MutatingPlanTransport()
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-secret", "session-secret"),
        transport=transport,
        clock=lambda: NOW,
    )
    rate_gate, concurrency_gate = _gates()

    with pytest.raises(
        MarketBookBatchTransportError,
        match="plan changed during provider dispatch",
    ):
        read_market_book_batch(
            client,
            plan,
            batch_id=batch.batch_id,
            request_id="plan-race",
            scheduled_at=NOW,
            rate_gate=rate_gate,
            concurrency_gate=concurrency_gate,
        )

    assert transport.calls == 1
    assert concurrency_gate.snapshot().active == ()
    rate_state = rate_gate.snapshot()
    assert len(rate_state.markets) == len(batch.market_ids)


def test_attempt_executor_freezes_pre_dispatch_plan_for_durable_history():
    plan = _plan(
        price_data=("EX_BEST_OFFERS",),
        best_prices_depth=1,
    )
    batch = plan.batches[0]

    class MutatingPlanTransport:
        def __init__(self) -> None:
            self.calls = 0

        def post(self, url, *, headers, body, timeout_seconds):
            self.calls += 1
            object.__setattr__(plan, "best_prices_depth", 2)
            return _payload(batch.market_ids)

    transport = MutatingPlanTransport()
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-secret", "session-secret"),
        transport=transport,
        clock=lambda: NOW,
    )
    rate_gate, concurrency_gate = _gates()

    execution = execute_market_book_batch_attempt(
        client,
        MarketBookAttemptHistory(plan, ()),
        batch_id=batch.batch_id,
        attempt_id="attempt-plan-snapshot",
        required=True,
        request_id="plan-snapshot",
        scheduled_at=NOW,
        rate_gate=rate_gate,
        concurrency_gate=concurrency_gate,
    )

    assert transport.calls == 1
    assert execution.outcome is MarketBookAttemptOutcome.EXACT_RESPONSE
    assert execution.result is not None
    assert execution.history.plan.best_prices_depth == 1
    assert execution.history.plan.plan_id == execution.result.plan_id
    assert plan.best_prices_depth == 2


def test_attempt_executor_records_post_dispatch_lease_cleanup_failure():
    plan = _plan(
        market_ids=("1.001",),
        order_projection="EXECUTABLE",
    )
    batch = plan.batches[0]
    history = MarketBookAttemptHistory(plan, ())
    rate_gate, concurrency_gate = _gates()

    class CompletingThenReturningTransport:
        def post(self, url, *, headers, body, timeout_seconds):
            active = concurrency_gate.snapshot().active
            assert len(active) == 1
            lease = active[0]
            concurrency_gate.complete(
                lease.request_id,
                lease_generation=lease.generation,
                observed_at=datetime.now(timezone.utc),
            )
            return _payload(batch.market_ids)

    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-secret", "session-secret"),
        transport=CompletingThenReturningTransport(),
        clock=lambda: NOW,
    )

    execution = execute_market_book_batch_attempt(
        client,
        history,
        batch_id=batch.batch_id,
        attempt_id="attempt-post-dispatch-cleanup",
        required=True,
        request_id="post-dispatch-cleanup",
        scheduled_at=NOW,
        rate_gate=rate_gate,
        concurrency_gate=concurrency_gate,
    )

    assert execution.outcome is MarketBookAttemptOutcome.TRANSPORT_FAILURE
    assert execution.result is None
    assert execution.history.required_gap_attempt_ids == (
        "attempt-post-dispatch-cleanup",
    )
    assert concurrency_gate.snapshot().active == ()
    assert len(rate_gate.snapshot().markets[0].accepted_at_utc_us) == 1


def test_large_plan_uses_budget_partition_and_dispatches_only_named_batch():
    market_ids = tuple(f"1.{index:03d}" for index in range(41))
    plan = _plan(market_ids=market_ids, price_data=("EX_BEST_OFFERS",))
    assert len(plan.batches) == 2
    assert len(plan.batches[0].market_ids) == 40
    assert len(plan.batches[1].market_ids) == 1
    second = plan.batches[1]
    client, transport = _client(_payload(second.market_ids))

    result = _read(client, plan, batch_id=second.batch_id)

    assert result.receipt.status is BatchReceiptStatus.EXACT_RESPONSE
    request = json.loads(transport.calls[0]["body"])
    assert request["params"]["marketIds"] == list(second.market_ids)
    assert request["params"]["priceProjection"] == {
        "priceData": ["EX_BEST_OFFERS"]
    }


def test_duplicate_provider_market_rows_fail_closed_as_noncanonical_receipt():
    plan = _plan()
    batch = plan.batches[0]
    client, _ = _client(_payload(("1.001", "1.001")))

    with pytest.raises(
        MarketBookBatchTransportError,
        match="cannot produce canonical structural receipt",
    ):
        _read(client, plan, batch_id=batch.batch_id)


def test_direct_transport_result_construction_cannot_mint_issuance():
    plan = _plan()
    batch = plan.batches[0]
    fake_receipt_client, _ = _client(_payload(batch.market_ids))
    issued = _read(
        fake_receipt_client,
        plan,
        batch_id=batch.batch_id,
    )
    forged = MarketBookBatchTransportResult(
        plan_id=issued.plan_id,
        request_contract_id=issued.request_contract_id,
        batch_id=issued.batch_id,
        request_budget_evidence_id=issued.request_budget_evidence_id,
        request_payload_sha256=issued.request_payload_sha256,
        source_payload_sha256=issued.source_payload_sha256,
        observed_at=issued.observed_at,
        canonical_network_origin=issued.canonical_network_origin,
        receipt=issued.receipt,
    )

    with pytest.raises(
        MarketBookBatchTransportError,
        match="not issued by canonical transport",
    ):
        forged.assert_issued()


def test_exact_transport_result_appends_to_canonical_attempt_history():
    plan = _plan()
    history = MarketBookAttemptHistory(plan, ())
    batch = plan.batches[0]
    client, _ = _client(_payload(batch.market_ids))
    result = _read(client, plan, batch_id=batch.batch_id)

    updated = append_market_book_transport_attempt(
        history,
        result,
        attempt_id="attempt-1",
        required=True,
    )

    assert len(updated.records) == 1
    assert updated.records[0].outcome is MarketBookAttemptOutcome.EXACT_RESPONSE
    assert updated.records[0].exact_receipt == result.receipt
    assert updated.current_structural_complete is True
    assert updated.required_gap_attempt_ids == ()
    assert updated.historical_required_gap_free is True


def test_incomplete_transport_result_preserves_required_gap_in_attempt_history():
    plan = _plan()
    history = MarketBookAttemptHistory(plan, ())
    batch = plan.batches[0]
    client, _ = _client(_payload(("1.001",)))
    result = _read(client, plan, batch_id=batch.batch_id)

    updated = append_market_book_transport_attempt(
        history,
        result,
        attempt_id="attempt-gap",
        required=True,
    )

    assert updated.records[0].outcome is MarketBookAttemptOutcome.INCOMPLETE_RESPONSE
    assert updated.records[0].exact_receipt is None
    assert updated.current_structural_complete is False
    assert updated.required_gap_attempt_ids == ("attempt-gap",)
    assert updated.historical_required_gap_free is False


def test_forged_transport_result_cannot_be_appended_to_attempt_history():
    plan = _plan()
    history = MarketBookAttemptHistory(plan, ())
    batch = plan.batches[0]
    client, _ = _client(_payload(batch.market_ids))
    issued = _read(client, plan, batch_id=batch.batch_id)
    forged = replace(issued)

    with pytest.raises(
        MarketBookBatchTransportError,
        match="not issued by canonical transport",
    ):
        append_market_book_transport_attempt(
            history,
            forged,
            attempt_id="attempt-forged",
            required=True,
        )


def test_class_rebound_assert_issued_cannot_append_forged_result(monkeypatch):
    plan = _plan()
    history = MarketBookAttemptHistory(plan, ())
    batch = plan.batches[0]
    client, _ = _client(_payload(batch.market_ids))
    issued = _read(client, plan, batch_id=batch.batch_id)
    forged = replace(issued)

    monkeypatch.setattr(
        MarketBookBatchTransportResult,
        "assert_issued",
        lambda self: None,
    )

    with pytest.raises(
        MarketBookBatchTransportError,
        match="not issued by canonical transport",
    ):
        append_market_book_transport_attempt(
            history,
            forged,
            attempt_id="attempt-forged-class-rebind",
            required=True,
        )

    assert history.records == ()



def test_class_rebound_assert_issued_cannot_forge_attempt_execution(monkeypatch):
    plan = _plan()
    batch = plan.batches[0]
    client, _ = _client(_payload(batch.market_ids))
    issued = _read(client, plan, batch_id=batch.batch_id)
    history = append_market_book_transport_attempt(
        MarketBookAttemptHistory(plan, ()),
        issued,
        attempt_id="attempt-issued-execution",
        required=True,
    )
    forged = replace(issued)

    monkeypatch.setattr(
        MarketBookBatchTransportResult,
        "assert_issued",
        lambda self: None,
    )

    with pytest.raises(
        MarketBookBatchTransportError,
        match="not issued by canonical transport",
    ):
        MarketBookBatchAttemptExecution(
            history,
            MarketBookAttemptOutcome.EXACT_RESPONSE,
            forged,
        )

def test_invalid_client_fails_before_gate_mutation():
    plan = _plan()
    batch = plan.batches[0]
    rate_gate, concurrency_gate = _gates()

    with pytest.raises(TypeError, match="client must be an exact BetfairReadOnlyClient"):
        read_market_book_batch(
            object(),
            plan,
            batch_id=batch.batch_id,
            request_id="invalid-client",
            scheduled_at=NOW,
            rate_gate=rate_gate,
            concurrency_gate=concurrency_gate,
        )

    assert rate_gate.snapshot().markets == ()
    assert concurrency_gate.snapshot().active == ()


def test_instance_shadowed_concurrency_begin_cannot_bypass_full_gate(monkeypatch):
    plan = _plan(order_projection="EXECUTABLE")
    batch = plan.batches[0]
    client, transport = _client(_payload(batch.market_ids))
    rate_gate, concurrency_gate = _gates()
    for index in range(3):
        decision = BetfairMarketBookProjectionConcurrencyGate.begin(
            concurrency_gate,
            f"held-{index}",
            observed_at=NOW,
            has_order_projection=True,
            has_match_projection=False,
        )
        assert decision.allowed is True

    shadow_called = False

    class ForgedDecision:
        allowed = True
        lease_generation = None

    def forged_begin(*args, **kwargs):
        nonlocal shadow_called
        shadow_called = True
        return ForgedDecision()

    monkeypatch.setattr(concurrency_gate, "begin", forged_begin)

    with pytest.raises(MarketBookBatchAdmissionError) as exc_info:
        read_market_book_batch(
            client,
            plan,
            batch_id=batch.batch_id,
            request_id="shadowed-concurrency",
            scheduled_at=NOW,
            rate_gate=rate_gate,
            concurrency_gate=concurrency_gate,
        )

    assert exc_info.value.outcome is MarketBookAttemptOutcome.NOT_DISPATCHED_CONCURRENCY
    assert shadow_called is False
    assert transport.calls == []
    assert len(concurrency_gate.snapshot().active) == 3


def test_instance_shadowed_rate_reserve_cannot_bypass_full_gate(monkeypatch):
    plan = _plan(market_ids=("1.001",))
    batch = plan.batches[0]
    client, transport = _client(_payload(batch.market_ids))
    rate_gate, concurrency_gate = _gates()
    for _ in range(5):
        assert BetfairMarketBookPerMarketRateGate.reserve(
            rate_gate,
            ("1.001",),
            scheduled_at=NOW,
        ).allowed is True

    shadow_called = False

    class ForgedDecision:
        allowed = True

    def forged_reserve(*args, **kwargs):
        nonlocal shadow_called
        shadow_called = True
        return ForgedDecision()

    monkeypatch.setattr(rate_gate, "reserve", forged_reserve)

    with pytest.raises(MarketBookBatchAdmissionError) as exc_info:
        read_market_book_batch(
            client,
            plan,
            batch_id=batch.batch_id,
            request_id="shadowed-rate",
            scheduled_at=NOW,
            rate_gate=rate_gate,
            concurrency_gate=concurrency_gate,
        )

    assert exc_info.value.outcome is MarketBookAttemptOutcome.NOT_DISPATCHED_RATE
    assert shadow_called is False
    assert transport.calls == []


def test_projection_concurrency_denial_precedes_rate_reservation_and_transport():
    plan = _plan(order_projection="EXECUTABLE")
    batch = plan.batches[0]
    client, transport = _client(_payload(batch.market_ids))
    rate_gate = BetfairMarketBookPerMarketRateGate()
    concurrency_gate = BetfairMarketBookProjectionConcurrencyGate()
    for index in range(3):
        decision = concurrency_gate.begin(
            f"occupied-{index}",
            observed_at=NOW,
            has_order_projection=True,
            has_match_projection=False,
        )
        assert decision.allowed is True

    with pytest.raises(MarketBookBatchAdmissionError) as exc_info:
        read_market_book_batch(
            client,
            plan,
            batch_id=batch.batch_id,
            request_id="blocked-concurrency",
            scheduled_at=NOW,
            rate_gate=rate_gate,
            concurrency_gate=concurrency_gate,
        )

    assert exc_info.value.outcome is MarketBookAttemptOutcome.NOT_DISPATCHED_CONCURRENCY
    assert rate_gate.snapshot().markets == ()
    assert transport.calls == []


def test_attempt_executor_records_concurrency_clock_regression_without_transport():
    plan = _plan(market_ids=("1.001",))
    batch = plan.batches[0]
    client, transport = _client(_payload(batch.market_ids))
    rate_gate, concurrency_gate = _gates()
    primed = concurrency_gate.begin(
        "future-clock-prime",
        observed_at=NOW + timedelta(seconds=1),
        has_order_projection=False,
        has_match_projection=False,
    )
    assert primed.allowed is True

    execution = execute_market_book_batch_attempt(
        client,
        MarketBookAttemptHistory(plan, ()),
        batch_id=batch.batch_id,
        attempt_id="attempt-concurrency-clock",
        required=True,
        request_id="concurrency-clock",
        scheduled_at=NOW,
        rate_gate=rate_gate,
        concurrency_gate=concurrency_gate,
    )

    assert execution.outcome is MarketBookAttemptOutcome.NOT_DISPATCHED_CONCURRENCY
    assert execution.result is None
    assert execution.history.required_gap_attempt_ids == (
        "attempt-concurrency-clock",
    )
    assert rate_gate.snapshot().markets == ()
    assert transport.calls == []


def test_attempt_executor_records_rate_clock_regression_without_transport():
    plan = _plan(market_ids=("1.001",))
    batch = plan.batches[0]
    client, transport = _client(_payload(batch.market_ids))
    rate_gate, concurrency_gate = _gates()
    assert rate_gate.reserve(
        batch.market_ids,
        scheduled_at=NOW + timedelta(seconds=1),
    ).allowed is True

    execution = execute_market_book_batch_attempt(
        client,
        MarketBookAttemptHistory(plan, ()),
        batch_id=batch.batch_id,
        attempt_id="attempt-rate-clock",
        required=True,
        request_id="rate-clock",
        scheduled_at=NOW,
        rate_gate=rate_gate,
        concurrency_gate=concurrency_gate,
    )

    assert execution.outcome is MarketBookAttemptOutcome.NOT_DISPATCHED_RATE
    assert execution.result is None
    assert execution.history.required_gap_attempt_ids == ("attempt-rate-clock",)
    assert transport.calls == []


def test_rate_gate_process_control_releases_projection_lease():
    plan = _plan(
        market_ids=("1.001",),
        order_projection="EXECUTABLE",
    )
    batch = plan.batches[0]
    client, transport = _client(_payload(batch.market_ids))
    rate_gate, concurrency_gate = _gates()
    interrupt = KeyboardInterrupt("rate admission stop")

    class InterruptingLock:
        def __enter__(self):
            raise interrupt

        def __exit__(self, exc_type, exc, tb):
            return False

    rate_gate._lock = InterruptingLock()

    with pytest.raises(KeyboardInterrupt) as exc_info:
        read_market_book_batch(
            client,
            plan,
            batch_id=batch.batch_id,
            request_id="rate-process-control",
            scheduled_at=NOW,
            rate_gate=rate_gate,
            concurrency_gate=concurrency_gate,
        )

    assert exc_info.value is interrupt
    assert transport.calls == []
    assert concurrency_gate.snapshot().active == ()
    assert rate_gate._accepted == {}
    assert rate_gate._last_scheduled_at_utc_us is None


def test_rate_denial_releases_projection_lease_without_transport():
    plan = _plan(
        market_ids=("1.001",),
        order_projection="EXECUTABLE",
    )
    batch = plan.batches[0]
    client, transport = _client(_payload(batch.market_ids))
    rate_gate = BetfairMarketBookPerMarketRateGate()
    concurrency_gate = BetfairMarketBookProjectionConcurrencyGate()
    admission_now = NOW
    for _ in range(5):
        assert rate_gate.reserve(
            ("1.001",),
            scheduled_at=admission_now,
        ).allowed is True

    with pytest.raises(MarketBookBatchAdmissionError) as exc_info:
        read_market_book_batch(
            client,
            plan,
            batch_id=batch.batch_id,
            request_id="rate-denied",
            scheduled_at=NOW,
            rate_gate=rate_gate,
            concurrency_gate=concurrency_gate,
        )

    assert exc_info.value.outcome is MarketBookAttemptOutcome.NOT_DISPATCHED_RATE
    assert concurrency_gate.snapshot().active == ()
    assert transport.calls == []


def test_historical_scheduled_at_cannot_bypass_physical_rate_window():
    plan = _plan(market_ids=("1.001",))
    batch = plan.batches[0]
    client, transport = _client(_payload(batch.market_ids))
    rate_gate, concurrency_gate = _gates()
    admission_now = NOW
    for _ in range(5):
        assert rate_gate.reserve(
            ("1.001",),
            scheduled_at=admission_now,
        ).allowed is True

    with pytest.raises(MarketBookBatchAdmissionError) as exc_info:
        read_market_book_batch(
            client,
            plan,
            batch_id=batch.batch_id,
            request_id="stale-schedule",
            scheduled_at=admission_now - timedelta(days=30),
            rate_gate=rate_gate,
            concurrency_gate=concurrency_gate,
        )

    assert exc_info.value.outcome is MarketBookAttemptOutcome.NOT_DISPATCHED_RATE
    assert transport.calls == []


def test_injected_transport_rate_gate_uses_canonical_client_clock():
    plan = _plan(market_ids=("1.001",))
    batch = plan.batches[0]
    client, transport = _client(_payload(batch.market_ids))
    rate_gate, concurrency_gate = _gates()

    result = read_market_book_batch(
        client,
        plan,
        batch_id=batch.batch_id,
        request_id="controlled-clock",
        scheduled_at=NOW - timedelta(days=30),
        rate_gate=rate_gate,
        concurrency_gate=concurrency_gate,
    )

    assert result.receipt.status is BatchReceiptStatus.EXACT_RESPONSE
    assert len(transport.calls) == 1
    state = rate_gate.snapshot()
    expected_us = int(NOW.timestamp() * 1_000_000)
    assert state.last_scheduled_at_utc_us == expected_us
    assert state.markets[0].accepted_at_utc_us == (expected_us,)


def test_successful_projection_read_releases_local_concurrency_lease():
    plan = _plan(order_projection="EXECUTABLE")
    batch = plan.batches[0]
    client, transport = _client(_payload(batch.market_ids))
    rate_gate, concurrency_gate = _gates()

    result = read_market_book_batch(
        client,
        plan,
        batch_id=batch.batch_id,
        request_id="projection-success",
        scheduled_at=NOW,
        rate_gate=rate_gate,
        concurrency_gate=concurrency_gate,
    )

    assert result.receipt.status is BatchReceiptStatus.EXACT_RESPONSE
    assert len(transport.calls) == 1
    assert concurrency_gate.snapshot().active == ()


def test_injected_projection_release_keeps_client_clock_authority():
    plan = _plan(
        market_ids=("1.001",),
        order_projection="EXECUTABLE",
    )
    batch = plan.batches[0]
    client, transport = _client(_payload(batch.market_ids))
    rate_gate, concurrency_gate = _gates()

    first = read_market_book_batch(
        client,
        plan,
        batch_id=batch.batch_id,
        request_id="projection-clock-1",
        scheduled_at=NOW,
        rate_gate=rate_gate,
        concurrency_gate=concurrency_gate,
    )
    second = read_market_book_batch(
        client,
        plan,
        batch_id=batch.batch_id,
        request_id="projection-clock-2",
        scheduled_at=NOW,
        rate_gate=rate_gate,
        concurrency_gate=concurrency_gate,
    )

    assert first.receipt.status is BatchReceiptStatus.EXACT_RESPONSE
    assert second.receipt.status is BatchReceiptStatus.EXACT_RESPONSE
    assert len(transport.calls) == 2
    state = concurrency_gate.snapshot()
    assert state.active == ()
    assert state.last_observed_at_utc_us == int(NOW.timestamp() * 1_000_000)


def test_attempt_executor_rejects_invalid_required_before_transport():
    plan = _plan(market_ids=("1.001",))
    batch = plan.batches[0]
    client, transport = _client(_payload(batch.market_ids))
    rate_gate, concurrency_gate = _gates()

    with pytest.raises(TypeError, match="required must be exact bool"):
        execute_market_book_batch_attempt(
            client,
            MarketBookAttemptHistory(plan, ()),
            batch_id=batch.batch_id,
            attempt_id="attempt-invalid-required",
            required=1,
            request_id="invalid-required",
            scheduled_at=NOW,
            rate_gate=rate_gate,
            concurrency_gate=concurrency_gate,
        )

    assert transport.calls == []
    assert rate_gate.snapshot().markets == ()
    assert concurrency_gate.snapshot().active == ()


def test_attempt_executor_rejects_duplicate_attempt_id_before_transport():
    plan = _plan(market_ids=("1.001",))
    batch = plan.batches[0]
    first_client, _ = _client(_payload(batch.market_ids))
    first_rate, first_concurrency = _gates()
    first = execute_market_book_batch_attempt(
        first_client,
        MarketBookAttemptHistory(plan, ()),
        batch_id=batch.batch_id,
        attempt_id="attempt-stable",
        required=True,
        request_id="first-request",
        scheduled_at=NOW,
        rate_gate=first_rate,
        concurrency_gate=first_concurrency,
    )
    assert first.outcome is MarketBookAttemptOutcome.EXACT_RESPONSE

    second_client, second_transport = _client(_payload(batch.market_ids))
    second_rate, second_concurrency = _gates()
    with pytest.raises(
        MarketBookBatchTransportError,
        match="attempt_id is already present",
    ):
        execute_market_book_batch_attempt(
            second_client,
            first.history,
            batch_id=batch.batch_id,
            attempt_id="attempt-stable",
            required=True,
            request_id="duplicate-attempt",
            scheduled_at=NOW,
            rate_gate=second_rate,
            concurrency_gate=second_concurrency,
        )

    assert second_transport.calls == []
    assert second_rate.snapshot().markets == ()
    assert second_concurrency.snapshot().active == ()


def test_direct_attempt_execution_cannot_claim_unrecorded_or_mismatched_outcome():
    plan = _plan(market_ids=("1.001",))
    empty = MarketBookAttemptHistory(plan, ())

    with pytest.raises(
        MarketBookBatchTransportError,
        match="requires an appended canonical history record",
    ):
        MarketBookBatchAttemptExecution(
            empty,
            MarketBookAttemptOutcome.TRANSPORT_FAILURE,
            None,
        )

    batch = plan.batches[0]
    client, _ = _client(_payload(batch.market_ids))
    issued = _read(client, plan, batch_id=batch.batch_id)
    recorded = append_market_book_transport_attempt(
        empty,
        issued,
        attempt_id="attempt-recorded",
        required=True,
    )

    with pytest.raises(
        MarketBookBatchTransportError,
        match="does not match latest history record",
    ):
        MarketBookBatchAttemptExecution(
            recorded,
            MarketBookAttemptOutcome.TRANSPORT_FAILURE,
            None,
        )


def test_attempt_execution_rejects_exact_result_for_incomplete_outcome():
    plan = _plan()
    batch = plan.batches[0]

    incomplete_client, _ = _client(_payload(("1.001",)))
    incomplete_result = _read(
        incomplete_client,
        plan,
        batch_id=batch.batch_id,
        request_id="incomplete-result",
    )
    recorded = append_market_book_transport_attempt(
        MarketBookAttemptHistory(plan, ()),
        incomplete_result,
        attempt_id="attempt-incomplete",
        required=True,
    )
    assert recorded.records[-1].outcome is MarketBookAttemptOutcome.INCOMPLETE_RESPONSE

    exact_client, _ = _client(_payload(batch.market_ids))
    exact_result = _read(
        exact_client,
        plan,
        batch_id=batch.batch_id,
        request_id="exact-result",
    )
    assert exact_result.receipt.status is BatchReceiptStatus.EXACT_RESPONSE

    with pytest.raises(
        MarketBookBatchTransportError,
        match="receipt status contradicts outcome",
    ):
        MarketBookBatchAttemptExecution(
            recorded,
            MarketBookAttemptOutcome.INCOMPLETE_RESPONSE,
            exact_result,
        )


def test_attempt_execution_revalidates_history_after_adversarial_plan_mutation():
    plan = _plan()
    history = MarketBookAttemptHistory(plan, ())
    batch = plan.batches[0]
    client, _ = _client(_payload(("1.001",)))
    result = _read(client, plan, batch_id=batch.batch_id)
    recorded = append_market_book_transport_attempt(
        history,
        result,
        attempt_id="attempt-incomplete-bound",
        required=True,
    )
    assert recorded.records[-1].outcome is MarketBookAttemptOutcome.INCOMPLETE_RESPONSE

    object.__setattr__(
        recorded,
        "plan",
        _plan(market_ids=("9.999",)),
    )

    with pytest.raises(
        MarketBookBatchTransportError,
        match="history is not canonical",
    ):
        MarketBookBatchAttemptExecution(
            recorded,
            MarketBookAttemptOutcome.INCOMPLETE_RESPONSE,
            result,
        )


def test_attempt_executor_records_rate_denial_as_required_gap():
    plan = _plan(market_ids=("1.001",))
    history = MarketBookAttemptHistory(plan, ())
    batch = plan.batches[0]
    client, transport = _client(_payload(batch.market_ids))
    rate_gate, concurrency_gate = _gates()
    admission_now = NOW
    for _ in range(5):
        assert rate_gate.reserve(
            ("1.001",),
            scheduled_at=admission_now,
        ).allowed is True

    execution = execute_market_book_batch_attempt(
        client,
        history,
        batch_id=batch.batch_id,
        attempt_id="attempt-rate",
        required=True,
        request_id="attempt-rate-request",
        scheduled_at=NOW,
        rate_gate=rate_gate,
        concurrency_gate=concurrency_gate,
    )

    assert execution.outcome is MarketBookAttemptOutcome.NOT_DISPATCHED_RATE
    assert execution.result is None
    assert execution.history.required_gap_attempt_ids == ("attempt-rate",)
    assert transport.calls == []


def test_attempt_executor_records_unexpected_post_response_finalization_failure(
    monkeypatch,
):
    plan = _plan(
        market_ids=("1.001",),
        order_projection="EXECUTABLE",
    )
    batch = plan.batches[0]
    client, transport = _client(_payload(batch.market_ids))
    rate_gate, concurrency_gate = _gates()

    def fail_finalization(self):
        raise RuntimeError("synthetic post-response finalizer failure")

    monkeypatch.setattr(
        MarketBookBatchTransportResult,
        "__post_init__",
        fail_finalization,
    )

    execution = execute_market_book_batch_attempt(
        client,
        MarketBookAttemptHistory(plan, ()),
        batch_id=batch.batch_id,
        attempt_id="attempt-finalizer-failure",
        required=True,
        request_id="finalizer-failure",
        scheduled_at=NOW,
        rate_gate=rate_gate,
        concurrency_gate=concurrency_gate,
    )

    assert execution.outcome is MarketBookAttemptOutcome.TRANSPORT_FAILURE
    assert execution.result is None
    assert execution.history.required_gap_attempt_ids == (
        "attempt-finalizer-failure",
    )
    assert len(transport.calls) == 1
    assert concurrency_gate.snapshot().active == ()
    rate_state = rate_gate.snapshot()
    assert len(rate_state.markets) == 1
    assert len(rate_state.markets[0].accepted_at_utc_us) == 1


def test_attempt_executor_records_transport_failure_and_releases_projection_lease():
    class RaisingTransport:
        def post(self, url, *, headers, body, timeout_seconds):
            raise BetfairReadOnlyError("network failed")

    plan = _plan(
        market_ids=("1.001",),
        order_projection="EXECUTABLE",
    )
    history = MarketBookAttemptHistory(plan, ())
    batch = plan.batches[0]
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-secret", "session-secret"),
        transport=RaisingTransport(),
        clock=lambda: NOW,
    )
    rate_gate, concurrency_gate = _gates()

    execution = execute_market_book_batch_attempt(
        client,
        history,
        batch_id=batch.batch_id,
        attempt_id="attempt-transport",
        required=True,
        request_id="transport-failure",
        scheduled_at=NOW,
        rate_gate=rate_gate,
        concurrency_gate=concurrency_gate,
    )

    assert execution.outcome is MarketBookAttemptOutcome.TRANSPORT_FAILURE
    assert execution.result is None
    assert execution.history.required_gap_attempt_ids == ("attempt-transport",)
    assert concurrency_gate.snapshot().active == ()
    state = rate_gate.snapshot()
    assert state.markets[0].market_id == "1.001"
    assert len(state.markets[0].accepted_at_utc_us) == 1


def test_transport_failure_is_not_masked_by_projection_cleanup_failure():
    plan = _plan(
        market_ids=("1.001",),
        order_projection="EXECUTABLE",
    )
    history = MarketBookAttemptHistory(plan, ())
    batch = plan.batches[0]
    rate_gate, concurrency_gate = _gates()

    class CompletingThenFailingTransport:
        def post(self, url, *, headers, body, timeout_seconds):
            active = concurrency_gate.snapshot().active
            assert len(active) == 1
            lease = active[0]
            concurrency_gate.complete(
                lease.request_id,
                lease_generation=lease.generation,
                observed_at=datetime.now(timezone.utc),
            )
            raise BetfairReadOnlyError("network failed after external lease cleanup")

    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-secret", "session-secret"),
        transport=CompletingThenFailingTransport(),
        clock=lambda: NOW,
    )

    execution = execute_market_book_batch_attempt(
        client,
        history,
        batch_id=batch.batch_id,
        attempt_id="attempt-cleanup-race",
        required=True,
        request_id="cleanup-race",
        scheduled_at=NOW,
        rate_gate=rate_gate,
        concurrency_gate=concurrency_gate,
    )

    assert execution.outcome is MarketBookAttemptOutcome.TRANSPORT_FAILURE
    assert execution.result is None
    assert execution.history.required_gap_attempt_ids == ("attempt-cleanup-race",)
    assert concurrency_gate.snapshot().active == ()


@pytest.mark.parametrize(
    "interrupt",
    (
        KeyboardInterrupt("operator stop"),
        SystemExit(17),
    ),
)
def test_process_control_interrupt_releases_projection_lease_and_propagates_unchanged(
    interrupt,
):
    plan = _plan(
        market_ids=("1.001",),
        order_projection="EXECUTABLE",
    )
    history = MarketBookAttemptHistory(plan, ())
    batch = plan.batches[0]
    rate_gate, concurrency_gate = _gates()

    class InterruptingTransport:
        def __init__(self):
            self.calls = 0

        def post(self, url, *, headers, body, timeout_seconds):
            self.calls += 1
            raise interrupt

    transport = InterruptingTransport()
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-secret", "session-secret"),
        transport=transport,
        clock=lambda: NOW,
    )

    with pytest.raises(type(interrupt)) as exc_info:
        execute_market_book_batch_attempt(
            client,
            history,
            batch_id=batch.batch_id,
            attempt_id="attempt-process-control",
            required=True,
            request_id="process-control",
            scheduled_at=NOW,
            rate_gate=rate_gate,
            concurrency_gate=concurrency_gate,
        )

    assert exc_info.value is interrupt
    assert transport.calls == 1
    assert history.records == ()
    assert concurrency_gate.snapshot().active == ()
    state = rate_gate.snapshot()
    assert state.markets[0].market_id == "1.001"
    assert len(state.markets[0].accepted_at_utc_us) == 1


def test_success_cleanup_process_control_interrupt_retries_release(
    monkeypatch,
):
    plan = _plan(
        market_ids=("1.001",),
        order_projection="EXECUTABLE",
    )
    batch = plan.batches[0]
    client, transport = _client(_payload(batch.market_ids))
    rate_gate, concurrency_gate = _gates()
    original_complete = concurrency_gate.complete
    interrupt = KeyboardInterrupt("cleanup stop")
    calls = 0

    def interrupt_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise interrupt
        return original_complete(*args, **kwargs)

    monkeypatch.setattr(concurrency_gate, "complete", interrupt_once)

    with pytest.raises(KeyboardInterrupt) as exc_info:
        read_market_book_batch(
            client,
            plan,
            batch_id=batch.batch_id,
            request_id="cleanup-process-control",
            scheduled_at=NOW,
            rate_gate=rate_gate,
            concurrency_gate=concurrency_gate,
        )

    assert exc_info.value is interrupt
    assert calls == 2
    assert len(transport.calls) == 1
    assert concurrency_gate.snapshot().active == ()


def test_cleanup_process_control_supersedes_regular_transport_failure(
    monkeypatch,
):
    plan = _plan(
        market_ids=("1.001",),
        order_projection="EXECUTABLE",
    )
    batch = plan.batches[0]
    rate_gate, concurrency_gate = _gates()
    cleanup_interrupt = KeyboardInterrupt("cleanup interrupt")

    class FailingTransport:
        def post(self, url, *, headers, body, timeout_seconds):
            raise BetfairReadOnlyError("network failed")

    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-secret", "session-secret"),
        transport=FailingTransport(),
        clock=lambda: NOW,
    )

    def interrupted_complete(*args, **kwargs):
        raise cleanup_interrupt

    monkeypatch.setattr(concurrency_gate, "complete", interrupted_complete)

    with pytest.raises(KeyboardInterrupt) as exc_info:
        read_market_book_batch(
            client,
            plan,
            batch_id=batch.batch_id,
            request_id="cleanup-precedence",
            scheduled_at=NOW,
            rate_gate=rate_gate,
            concurrency_gate=concurrency_gate,
        )

    assert exc_info.value is cleanup_interrupt
    assert len(concurrency_gate.snapshot().active) == 1


def test_attempt_executor_records_provider_and_protocol_failures():
    plan = _plan(market_ids=("1.001",))
    batch = plan.batches[0]

    provider_payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "error": {"code": -32099, "message": "provider rejected"},
            "id": 1,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    provider_client, _ = _client(provider_payload)
    rate_gate, concurrency_gate = _gates()
    provider_execution = execute_market_book_batch_attempt(
        provider_client,
        MarketBookAttemptHistory(plan, ()),
        batch_id=batch.batch_id,
        attempt_id="attempt-provider",
        required=True,
        request_id="provider-failure",
        scheduled_at=NOW,
        rate_gate=rate_gate,
        concurrency_gate=concurrency_gate,
    )
    assert provider_execution.outcome is MarketBookAttemptOutcome.PROVIDER_FAILURE

    protocol_client, _ = _client(b"{")
    rate_gate, concurrency_gate = _gates()
    protocol_execution = execute_market_book_batch_attempt(
        protocol_client,
        MarketBookAttemptHistory(plan, ()),
        batch_id=batch.batch_id,
        attempt_id="attempt-protocol",
        required=True,
        request_id="protocol-failure",
        scheduled_at=NOW,
        rate_gate=rate_gate,
        concurrency_gate=concurrency_gate,
    )
    assert protocol_execution.outcome is MarketBookAttemptOutcome.PARSE_FAILURE


def test_immediate_dispatch_rejects_future_causal_instant_before_any_gate_mutation():
    plan = _plan()
    batch = plan.batches[0]
    client, transport = _client(_payload(batch.market_ids))
    rate_gate, concurrency_gate = _gates()
    future = NOW + timedelta(microseconds=1)

    with pytest.raises(ValueError, match="must not be in the future"):
        read_market_book_batch(
            client,
            plan,
            batch_id=batch.batch_id,
            request_id="future",
            scheduled_at=future,
            rate_gate=rate_gate,
            concurrency_gate=concurrency_gate,
        )

    assert rate_gate.snapshot().markets == ()
    assert concurrency_gate.snapshot().active == ()
    assert transport.calls == []
