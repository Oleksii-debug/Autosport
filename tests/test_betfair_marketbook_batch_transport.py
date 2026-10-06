from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest

import autosport.betfair_marketbook_batch_transport as _batch_transport_module
import autosport.betfair_marketbook_batch_completeness as _batch_completeness_module
import autosport.betfair_marketbook_rate_gate as _rate_gate_module
import autosport.betfair_marketbook_projection_concurrency as _projection_gate_module
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



def test_class_rebound_authority_fingerprint_cannot_bless_mutated_result(monkeypatch):
    plan = _plan()
    batch = plan.batches[0]
    client, _ = _client(_payload(batch.market_ids))
    rebound_called = False

    def forged_fingerprint(self):
        nonlocal rebound_called
        rebound_called = True
        return "0" * 64

    monkeypatch.setattr(
        MarketBookBatchTransportResult,
        "_authority_fingerprint",
        forged_fingerprint,
    )

    issued = _read(client, plan, batch_id=batch.batch_id)
    issued.assert_issued()
    assert rebound_called is False

    object.__setattr__(issued, "request_payload_sha256", "0" * 64)
    with pytest.raises(
        MarketBookBatchTransportError,
        match="changed after canonical issuance",
    ):
        issued.assert_issued()

    assert rebound_called is False


def test_class_rebound_assert_issued_cannot_mutate_valid_append_semantics(monkeypatch):
    plan = _plan()
    history = MarketBookAttemptHistory(plan, ())
    batch = plan.batches[0]
    client, _ = _client(_payload(batch.market_ids))
    issued = _read(client, plan, batch_id=batch.batch_id)
    rebound_called = False

    def mutate_after_registry_proof(self):
        nonlocal rebound_called
        rebound_called = True
        object.__setattr__(
            self.receipt,
            "status",
            BatchReceiptStatus.INCOMPLETE_RESPONSE,
        )

    monkeypatch.setattr(
        MarketBookBatchTransportResult,
        "assert_issued",
        mutate_after_registry_proof,
    )

    updated = append_market_book_transport_attempt(
        history,
        issued,
        attempt_id="attempt-valid-class-rebind",
        required=True,
    )

    assert rebound_called is False
    assert issued.receipt.status is BatchReceiptStatus.EXACT_RESPONSE
    assert updated.records[-1].outcome is MarketBookAttemptOutcome.EXACT_RESPONSE
    assert updated.records[-1].exact_receipt == issued.receipt


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

def test_class_rebound_assert_issued_cannot_mutate_valid_execution_semantics(monkeypatch):
    plan = _plan()
    batch = plan.batches[0]
    client, _ = _client(_payload(batch.market_ids))
    issued = _read(client, plan, batch_id=batch.batch_id)
    history = append_market_book_transport_attempt(
        MarketBookAttemptHistory(plan, ()),
        issued,
        attempt_id="attempt-valid-execution-class-rebind",
        required=True,
    )
    rebound_called = False

    def mutate_after_registry_proof(self):
        nonlocal rebound_called
        rebound_called = True
        object.__setattr__(
            self.receipt,
            "status",
            BatchReceiptStatus.INCOMPLETE_RESPONSE,
        )

    monkeypatch.setattr(
        MarketBookBatchTransportResult,
        "assert_issued",
        mutate_after_registry_proof,
    )

    execution = MarketBookBatchAttemptExecution(
        history,
        MarketBookAttemptOutcome.EXACT_RESPONSE,
        issued,
    )

    assert rebound_called is False
    assert issued.receipt.status is BatchReceiptStatus.EXACT_RESPONSE
    assert execution.result is issued
    assert execution.outcome is MarketBookAttemptOutcome.EXACT_RESPONSE


def test_module_rebound_core_read_cannot_replace_canonical_transport(monkeypatch):
    plan = _plan(market_ids=("1.001",))
    batch = plan.batches[0]
    client, transport = _client(_payload(batch.market_ids))
    rate_gate, concurrency_gate = _gates()
    called = False

    def forged_core(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("rebound core read must not run")

    monkeypatch.setattr(
        _batch_transport_module,
        "_read_market_book_batch",
        forged_core,
    )

    result = read_market_book_batch(
        client,
        plan,
        batch_id=batch.batch_id,
        request_id="sealed-core-read",
        scheduled_at=NOW,
        rate_gate=rate_gate,
        concurrency_gate=concurrency_gate,
    )

    result.assert_issued()
    assert called is False
    assert len(transport.calls) == 1


def test_module_rebound_physical_post_cannot_replace_canonical_transport(monkeypatch):
    plan = _plan(market_ids=("1.001",))
    batch = plan.batches[0]
    client, transport = _client(_payload(batch.market_ids))
    rate_gate, concurrency_gate = _gates()
    called = False

    def forged_post(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("rebound physical post must not run")

    monkeypatch.setattr(
        _batch_transport_module._transport,
        "_post_market_book_readonly",
        forged_post,
    )

    result = read_market_book_batch(
        client,
        plan,
        batch_id=batch.batch_id,
        request_id="sealed-physical-post",
        scheduled_at=NOW,
        rate_gate=rate_gate,
        concurrency_gate=concurrency_gate,
    )

    result.assert_issued()
    assert called is False
    assert len(transport.calls) == 1


def test_module_rebound_result_factory_cannot_replace_canonical_evidence(monkeypatch):
    plan = _plan(market_ids=("1.001",))
    batch = plan.batches[0]
    client, transport = _client(_payload(batch.market_ids))
    rate_gate, concurrency_gate = _gates()
    called = False

    class ForgedResult:
        def __init__(self, *args, **kwargs):
            nonlocal called
            called = True
            raise AssertionError("rebound result factory must not run")

    monkeypatch.setattr(
        _batch_transport_module,
        "MarketBookBatchTransportResult",
        ForgedResult,
    )

    result = read_market_book_batch(
        client,
        plan,
        batch_id=batch.batch_id,
        request_id="sealed-result-factory",
        scheduled_at=NOW,
        rate_gate=rate_gate,
        concurrency_gate=concurrency_gate,
    )

    result.assert_issued()
    assert called is False
    assert type(result) is MarketBookBatchTransportResult
    assert len(transport.calls) == 1


def test_class_rebound_receipt_factory_cannot_replace_structural_evidence(monkeypatch):
    plan = _plan(market_ids=("1.001",))
    batch = plan.batches[0]
    client, transport = _client(_payload(batch.market_ids))
    rate_gate, concurrency_gate = _gates()
    called = False

    def forged_receipt(cls, *args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("rebound receipt factory must not run")

    monkeypatch.setattr(
        MarketBookBatchReceipt,
        "from_response",
        classmethod(forged_receipt),
    )

    result = read_market_book_batch(
        client,
        plan,
        batch_id=batch.batch_id,
        request_id="sealed-receipt-factory",
        scheduled_at=NOW,
        rate_gate=rate_gate,
        concurrency_gate=concurrency_gate,
    )

    result.assert_issued()
    assert called is False
    assert result.receipt.status is BatchReceiptStatus.EXACT_RESPONSE
    assert len(transport.calls) == 1


def test_module_rebound_canonical_batch_cannot_replace_plan_binding(monkeypatch):
    plan = _plan(market_ids=("1.001",))
    batch = plan.batches[0]
    client, transport = _client(_payload(batch.market_ids))
    rate_gate, concurrency_gate = _gates()
    called = False

    def forged_canonical_batch(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("rebound canonical batch must not run")

    monkeypatch.setattr(
        _batch_transport_module,
        "_canonical_batch",
        forged_canonical_batch,
    )

    result = read_market_book_batch(
        client,
        plan,
        batch_id=batch.batch_id,
        request_id="sealed-canonical-batch",
        scheduled_at=NOW,
        rate_gate=rate_gate,
        concurrency_gate=concurrency_gate,
    )

    result.assert_issued()
    assert called is False
    assert len(transport.calls) == 1


def test_module_rebound_params_for_batch_cannot_replace_request_contract(monkeypatch):
    plan = _plan(market_ids=("1.001",))
    batch = plan.batches[0]
    client, transport = _client(_payload(batch.market_ids))
    rate_gate, concurrency_gate = _gates()
    called = False

    def forged_params(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("rebound params mapper must not run")

    monkeypatch.setattr(
        _batch_transport_module,
        "_params_for_batch",
        forged_params,
    )

    result = read_market_book_batch(
        client,
        plan,
        batch_id=batch.batch_id,
        request_id="sealed-params-mapper",
        scheduled_at=NOW,
        rate_gate=rate_gate,
        concurrency_gate=concurrency_gate,
    )

    result.assert_issued()
    assert called is False
    assert len(transport.calls) == 1


def test_module_rebound_request_budget_cannot_replace_canonical_preflight(monkeypatch):
    plan = _plan(market_ids=("1.001",))
    batch = plan.batches[0]
    client, transport = _client(_payload(batch.market_ids))
    rate_gate, concurrency_gate = _gates()
    called = False

    def forged_budget(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("rebound request budget must not run")

    monkeypatch.setattr(
        _batch_transport_module._transport,
        "_market_book_request_budget",
        forged_budget,
    )

    result = read_market_book_batch(
        client,
        plan,
        batch_id=batch.batch_id,
        request_id="sealed-request-budget",
        scheduled_at=NOW,
        rate_gate=rate_gate,
        concurrency_gate=concurrency_gate,
    )

    result.assert_issued()
    assert called is False
    assert len(transport.calls) == 1


def test_attempt_executor_uses_sealed_read_delegate(monkeypatch):
    plan = _plan(market_ids=("1.001",))
    batch = plan.batches[0]
    client, transport = _client(_payload(batch.market_ids))
    rate_gate, concurrency_gate = _gates()
    called = False

    def rebound_read(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("module rebound read must not replace executor delegate")

    monkeypatch.setattr(
        _batch_transport_module,
        "read_market_book_batch",
        rebound_read,
    )

    execution = execute_market_book_batch_attempt(
        client,
        MarketBookAttemptHistory(plan, ()),
        batch_id=batch.batch_id,
        attempt_id="attempt-sealed-read",
        required=True,
        request_id="sealed-read",
        scheduled_at=NOW,
        rate_gate=rate_gate,
        concurrency_gate=concurrency_gate,
    )

    assert called is False
    assert execution.outcome is MarketBookAttemptOutcome.EXACT_RESPONSE
    assert len(transport.calls) == 1


def test_attempt_executor_uses_sealed_response_append_delegate(monkeypatch):
    plan = _plan(market_ids=("1.001",))
    batch = plan.batches[0]
    client, transport = _client(_payload(batch.market_ids))
    rate_gate, concurrency_gate = _gates()
    called = False

    def rebound_append(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("module rebound append must not replace executor delegate")

    monkeypatch.setattr(
        _batch_transport_module,
        "append_market_book_transport_attempt",
        rebound_append,
    )

    execution = execute_market_book_batch_attempt(
        client,
        MarketBookAttemptHistory(plan, ()),
        batch_id=batch.batch_id,
        attempt_id="attempt-sealed-append",
        required=True,
        request_id="sealed-append",
        scheduled_at=NOW,
        rate_gate=rate_gate,
        concurrency_gate=concurrency_gate,
    )

    assert called is False
    assert execution.history.records[-1].attempt_id == "attempt-sealed-append"
    assert execution.outcome is MarketBookAttemptOutcome.EXACT_RESPONSE
    assert len(transport.calls) == 1


def test_attempt_executor_uses_sealed_nonresponse_and_execution_type(monkeypatch):
    plan = _plan(market_ids=("1.001",))
    batch = plan.batches[0]
    client, transport = _client(_payload(batch.market_ids))
    rate_gate, concurrency_gate = _gates()
    for _ in range(5):
        assert rate_gate.reserve(("1.001",), scheduled_at=NOW).allowed is True
    called_nonresponse = False
    called_type = False

    def rebound_nonresponse(*args, **kwargs):
        nonlocal called_nonresponse
        called_nonresponse = True
        raise AssertionError("module rebound nonresponse append must not run")

    def rebound_execution_type(*args, **kwargs):
        nonlocal called_type
        called_type = True
        raise AssertionError("module rebound execution type must not run")

    monkeypatch.setattr(
        _batch_transport_module,
        "_append_nonresponse_attempt",
        rebound_nonresponse,
    )
    monkeypatch.setattr(
        _batch_transport_module,
        "MarketBookBatchAttemptExecution",
        rebound_execution_type,
    )

    execution = execute_market_book_batch_attempt(
        client,
        MarketBookAttemptHistory(plan, ()),
        batch_id=batch.batch_id,
        attempt_id="attempt-sealed-nonresponse",
        required=True,
        request_id="sealed-nonresponse",
        scheduled_at=NOW,
        rate_gate=rate_gate,
        concurrency_gate=concurrency_gate,
    )

    assert called_nonresponse is False
    assert called_type is False
    assert isinstance(execution, MarketBookBatchAttemptExecution)
    assert execution.outcome is MarketBookAttemptOutcome.NOT_DISPATCHED_RATE
    assert execution.history.required_gap_attempt_ids == (
        "attempt-sealed-nonresponse",
    )
    assert transport.calls == []


def test_attempt_executor_ignores_rebound_execution_post_init(monkeypatch):
    plan = _plan(market_ids=("1.001",))
    batch = plan.batches[0]
    client, transport = _client(_payload(batch.market_ids))
    rate_gate, concurrency_gate = _gates()
    rebound_called = False

    def rebound_post_init(self):
        nonlocal rebound_called
        rebound_called = True
        raise AssertionError("rebound execution validator must not run")

    monkeypatch.setattr(
        MarketBookBatchAttemptExecution,
        "__post_init__",
        rebound_post_init,
    )

    execution = execute_market_book_batch_attempt(
        client,
        MarketBookAttemptHistory(plan, ()),
        batch_id=batch.batch_id,
        attempt_id="attempt-sealed-execution-validator",
        required=True,
        request_id="sealed-execution-validator",
        scheduled_at=NOW,
        rate_gate=rate_gate,
        concurrency_gate=concurrency_gate,
    )

    assert rebound_called is False
    assert isinstance(execution, MarketBookBatchAttemptExecution)
    assert execution.outcome is MarketBookAttemptOutcome.EXACT_RESPONSE
    assert execution.result is not None
    assert execution.history.records[-1].attempt_id == (
        "attempt-sealed-execution-validator"
    )
    assert len(transport.calls) == 1


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


def test_rate_gate_post_mutation_process_control_rolls_back_capacity_and_lease():
    plan = _plan(
        market_ids=("1.001",),
        order_projection="EXECUTABLE",
    )
    batch = plan.batches[0]
    client, transport = _client(_payload(batch.market_ids))
    rate_gate, concurrency_gate = _gates()
    interrupt = KeyboardInterrupt("rate admission interrupted after mutation")
    original_lock = rate_gate._lock

    class InterruptAfterMutationLock:
        def __init__(self):
            self.raise_once = True

        def __enter__(self):
            return original_lock.__enter__()

        def __exit__(self, exc_type, exc, tb):
            result = original_lock.__exit__(exc_type, exc, tb)
            if self.raise_once and exc_type is None:
                self.raise_once = False
                raise interrupt
            return result

    rate_gate._lock = InterruptAfterMutationLock()

    with pytest.raises(KeyboardInterrupt) as exc_info:
        read_market_book_batch(
            client,
            plan,
            batch_id=batch.batch_id,
            request_id="rate-post-mutation-process-control",
            scheduled_at=NOW,
            rate_gate=rate_gate,
            concurrency_gate=concurrency_gate,
        )

    assert exc_info.value is interrupt
    assert transport.calls == []
    assert concurrency_gate.snapshot().active == ()
    rate_state = rate_gate.snapshot()
    assert rate_state.markets == ()
    assert rate_state.last_scheduled_at_utc_us == int(
        NOW.timestamp() * 1_000_000
    )


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


def test_instance_shadow_complete_cannot_bypass_projection_cleanup(monkeypatch):
    plan = _plan(
        market_ids=("1.001",),
        order_projection="EXECUTABLE",
    )
    batch = plan.batches[0]
    client, transport = _client(_payload(batch.market_ids))
    rate_gate, concurrency_gate = _gates()

    monkeypatch.setattr(
        concurrency_gate,
        "complete",
        lambda *args, **kwargs: None,
    )

    result = read_market_book_batch(
        client,
        plan,
        batch_id=batch.batch_id,
        request_id="shadowed-complete",
        scheduled_at=NOW,
        rate_gate=rate_gate,
        concurrency_gate=concurrency_gate,
    )

    assert result.receipt.status is BatchReceiptStatus.EXACT_RESPONSE
    assert len(transport.calls) == 1
    assert concurrency_gate.snapshot().active == ()


def test_success_cleanup_process_control_interrupt_retries_release():
    plan = _plan(
        market_ids=("1.001",),
        order_projection="EXECUTABLE",
    )
    batch = plan.batches[0]
    client, transport = _client(_payload(batch.market_ids))
    rate_gate, concurrency_gate = _gates()
    interrupt = KeyboardInterrupt("cleanup stop")
    original_lock = concurrency_gate._lock

    class InterruptCleanupLock:
        def __init__(self):
            self.armed = False
            self.raise_once = True
            self.entries = 0

        def __enter__(self):
            self.entries += 1
            return original_lock.__enter__()

        def __exit__(self, exc_type, exc, tb):
            result = original_lock.__exit__(exc_type, exc, tb)
            if self.armed and self.raise_once and exc_type is None:
                self.raise_once = False
                raise interrupt
            return result

    cleanup_lock = InterruptCleanupLock()
    concurrency_gate._lock = cleanup_lock

    original_post = client._transport.post

    def arm_cleanup_then_return(*args, **kwargs):
        response = original_post(*args, **kwargs)
        cleanup_lock.armed = True
        return response

    client._transport.post = arm_cleanup_then_return

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
    assert len(transport.calls) == 1
    assert cleanup_lock.entries >= 3
    assert concurrency_gate.snapshot().active == ()


def test_cleanup_process_control_supersedes_regular_transport_failure():
    plan = _plan(
        market_ids=("1.001",),
        order_projection="EXECUTABLE",
    )
    batch = plan.batches[0]
    rate_gate, concurrency_gate = _gates()
    cleanup_interrupt = KeyboardInterrupt("cleanup interrupt")
    original_lock = concurrency_gate._lock

    class InterruptCleanupLock:
        def __init__(self):
            self.armed = False
            self.raise_once = True

        def __enter__(self):
            return original_lock.__enter__()

        def __exit__(self, exc_type, exc, tb):
            result = original_lock.__exit__(exc_type, exc, tb)
            if self.armed and self.raise_once and exc_type is None:
                self.raise_once = False
                raise cleanup_interrupt
            return result

    cleanup_lock = InterruptCleanupLock()
    concurrency_gate._lock = cleanup_lock

    class FailingTransport:
        def post(self, url, *, headers, body, timeout_seconds):
            cleanup_lock.armed = True
            raise BetfairReadOnlyError("network failed")

    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-secret", "session-secret"),
        transport=FailingTransport(),
        clock=lambda: NOW,
    )

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
    assert concurrency_gate.snapshot().active == ()


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

def test_class_rebound_evidence_id_cannot_bless_mutated_result(monkeypatch):
    plan = _plan()
    batch = plan.batches[0]
    client, _ = _client(_payload(batch.market_ids))
    rebound_called = False

    def forged_evidence_id(self):
        nonlocal rebound_called
        rebound_called = True
        return "0" * 64

    monkeypatch.setattr(
        MarketBookBatchTransportResult,
        "evidence_id",
        property(forged_evidence_id),
    )

    issued = _read(client, plan, batch_id=batch.batch_id)
    issued.assert_issued()
    assert rebound_called is False

    object.__setattr__(issued, "request_payload_sha256", "0" * 64)
    with pytest.raises(
        MarketBookBatchTransportError,
        match="changed after canonical issuance",
    ):
        issued.assert_issued()

    assert rebound_called is False


def test_class_rebound_evidence_payload_cannot_bless_mutated_result(monkeypatch):
    plan = _plan()
    batch = plan.batches[0]
    client, _ = _client(_payload(batch.market_ids))
    rebound_called = False

    def forged_evidence_payload(self):
        nonlocal rebound_called
        rebound_called = True
        return {"forged": True}

    monkeypatch.setattr(
        MarketBookBatchTransportResult,
        "evidence_payload",
        property(forged_evidence_payload),
    )

    issued = _read(client, plan, batch_id=batch.batch_id)
    issued.assert_issued()
    assert rebound_called is False

    object.__setattr__(issued, "source_payload_sha256", "0" * 64)
    with pytest.raises(
        MarketBookBatchTransportError,
        match="changed after canonical issuance",
    ):
        issued.assert_issued()

    assert rebound_called is False


def test_module_rebound_dispatch_instant_cannot_reopen_full_rate_window(monkeypatch):
    plan = _plan(market_ids=("1.001",))
    batch = plan.batches[0]
    client, transport = _client(_payload(batch.market_ids))
    rate_gate, concurrency_gate = _gates()
    for _ in range(5):
        assert rate_gate.reserve(("1.001",), scheduled_at=NOW).allowed is True

    rebound_called = False

    def forged_dispatch_instant(*args, **kwargs):
        nonlocal rebound_called
        rebound_called = True
        return NOW + timedelta(seconds=2)

    monkeypatch.setattr(
        _batch_transport_module,
        "_dispatch_instant",
        forged_dispatch_instant,
    )

    with pytest.raises(MarketBookBatchAdmissionError) as exc_info:
        read_market_book_batch(
            client,
            plan,
            batch_id=batch.batch_id,
            request_id="sealed-dispatch-instant",
            scheduled_at=NOW,
            rate_gate=rate_gate,
            concurrency_gate=concurrency_gate,
        )

    assert exc_info.value.outcome is MarketBookAttemptOutcome.NOT_DISPATCHED_RATE
    assert rebound_called is False
    assert transport.calls == []


def test_module_rebound_transport_now_cannot_reopen_full_rate_window(monkeypatch):
    plan = _plan(market_ids=("1.001",))
    batch = plan.batches[0]
    client, transport = _client(_payload(batch.market_ids))
    rate_gate, concurrency_gate = _gates()
    for _ in range(5):
        assert rate_gate.reserve(("1.001",), scheduled_at=NOW).allowed is True

    rebound_called = False

    def forged_transport_now(*args, **kwargs):
        nonlocal rebound_called
        rebound_called = True
        return NOW + timedelta(seconds=2)

    monkeypatch.setattr(
        _batch_transport_module,
        "_transport_now",
        forged_transport_now,
    )

    with pytest.raises(MarketBookBatchAdmissionError) as exc_info:
        read_market_book_batch(
            client,
            plan,
            batch_id=batch.batch_id,
            request_id="sealed-transport-now",
            scheduled_at=NOW,
            rate_gate=rate_gate,
            concurrency_gate=concurrency_gate,
        )

    assert exc_info.value.outcome is MarketBookAttemptOutcome.NOT_DISPATCHED_RATE
    assert rebound_called is False
    assert transport.calls == []


def test_module_rebound_network_origin_probe_cannot_replace_transport_clock(monkeypatch):
    plan = _plan(market_ids=("1.001",))
    batch = plan.batches[0]
    client, transport = _client(_payload(batch.market_ids))
    rate_gate, concurrency_gate = _gates()
    rebound_called = False

    def forged_network_origin(*args, **kwargs):
        nonlocal rebound_called
        rebound_called = True
        raise AssertionError("rebound network-origin probe must not run")

    monkeypatch.setattr(
        _batch_transport_module._transport,
        "_canonical_network_transport",
        forged_network_origin,
    )

    result = read_market_book_batch(
        client,
        plan,
        batch_id=batch.batch_id,
        request_id="sealed-network-origin-probe",
        scheduled_at=NOW,
        rate_gate=rate_gate,
        concurrency_gate=concurrency_gate,
    )

    result.assert_issued()
    assert rebound_called is False
    assert len(transport.calls) == 1


def test_module_rebound_projection_cleanup_cannot_strand_success_lease(monkeypatch):
    plan = _plan(market_ids=("1.001",), order_projection="EXECUTABLE")
    batch = plan.batches[0]
    client, transport = _client(_payload(batch.market_ids))
    rate_gate, concurrency_gate = _gates()
    rebound_called = False

    def forged_release(*args, **kwargs):
        nonlocal rebound_called
        rebound_called = True
        raise AssertionError("rebound projection cleanup must not run")

    monkeypatch.setattr(
        _batch_transport_module,
        "_release_projection_lease",
        forged_release,
    )

    result = read_market_book_batch(
        client,
        plan,
        batch_id=batch.batch_id,
        request_id="sealed-success-cleanup",
        scheduled_at=NOW,
        rate_gate=rate_gate,
        concurrency_gate=concurrency_gate,
    )

    result.assert_issued()
    assert rebound_called is False
    assert concurrency_gate.snapshot().active == ()
    assert len(transport.calls) == 1


def test_module_rebound_failure_cleanup_cannot_mask_rate_denial_or_strand_lease(monkeypatch):
    plan = _plan(market_ids=("1.001",), order_projection="EXECUTABLE")
    batch = plan.batches[0]
    client, transport = _client(_payload(batch.market_ids))
    rate_gate, concurrency_gate = _gates()
    for _ in range(5):
        assert rate_gate.reserve(("1.001",), scheduled_at=NOW).allowed is True

    rebound_called = False

    def forged_cleanup(*args, **kwargs):
        nonlocal rebound_called
        rebound_called = True
        raise AssertionError("rebound failure cleanup must not run")

    monkeypatch.setattr(
        _batch_transport_module,
        "_release_projection_lease_after_failure",
        forged_cleanup,
    )

    with pytest.raises(MarketBookBatchAdmissionError) as exc_info:
        read_market_book_batch(
            client,
            plan,
            batch_id=batch.batch_id,
            request_id="sealed-failure-cleanup",
            scheduled_at=NOW,
            rate_gate=rate_gate,
            concurrency_gate=concurrency_gate,
        )

    assert exc_info.value.outcome is MarketBookAttemptOutcome.NOT_DISPATCHED_RATE
    assert rebound_called is False
    assert concurrency_gate.snapshot().active == ()
    assert transport.calls == []

def test_attempt_executor_seals_plan_roundtrip_methods(monkeypatch):
    plan = _plan(market_ids=("1.001",))
    batch = plan.batches[0]
    client, transport = _client(_payload(batch.market_ids))
    rate_gate, concurrency_gate = _gates()
    called_to = False
    called_from = False

    def forged_to_json(self):
        nonlocal called_to
        called_to = True
        raise AssertionError("rebound plan to_json must not run")

    def forged_from_json(cls, encoded):
        nonlocal called_from
        called_from = True
        raise AssertionError("rebound plan from_json must not run")

    monkeypatch.setattr(MarketBookReadPlan, "to_json", forged_to_json)
    monkeypatch.setattr(
        MarketBookReadPlan,
        "from_json",
        classmethod(forged_from_json),
    )

    execution = execute_market_book_batch_attempt(
        client,
        MarketBookAttemptHistory(plan, ()),
        batch_id=batch.batch_id,
        attempt_id="sealed-plan-roundtrip",
        required=True,
        request_id="sealed-plan-roundtrip",
        scheduled_at=NOW,
        rate_gate=rate_gate,
        concurrency_gate=concurrency_gate,
    )

    assert called_to is False
    assert called_from is False
    assert execution.outcome is MarketBookAttemptOutcome.EXACT_RESPONSE
    assert len(transport.calls) == 1


def test_attempt_executor_seals_history_roundtrip_methods(monkeypatch):
    plan = _plan(market_ids=("1.001",))
    batch = plan.batches[0]
    history = MarketBookAttemptHistory(plan, ())
    client, transport = _client(_payload(batch.market_ids))
    rate_gate, concurrency_gate = _gates()
    called_to = False
    called_from = False

    def forged_to_json(self):
        nonlocal called_to
        called_to = True
        raise AssertionError("rebound history to_json must not run")

    def forged_from_json(cls, plan_arg, encoded):
        nonlocal called_from
        called_from = True
        raise AssertionError("rebound history from_json must not run")

    monkeypatch.setattr(MarketBookAttemptHistory, "to_json", forged_to_json)
    monkeypatch.setattr(
        MarketBookAttemptHistory,
        "from_json",
        classmethod(forged_from_json),
    )

    execution = execute_market_book_batch_attempt(
        client,
        history,
        batch_id=batch.batch_id,
        attempt_id="sealed-history-roundtrip",
        required=True,
        request_id="sealed-history-roundtrip",
        scheduled_at=NOW,
        rate_gate=rate_gate,
        concurrency_gate=concurrency_gate,
    )

    assert called_to is False
    assert called_from is False
    assert execution.outcome is MarketBookAttemptOutcome.EXACT_RESPONSE
    assert len(transport.calls) == 1


def test_attempt_executor_seals_attempt_token_validation(monkeypatch):
    plan = _plan(market_ids=("1.001",))
    batch = plan.batches[0]
    client, transport = _client(_payload(batch.market_ids))
    rate_gate, concurrency_gate = _gates()
    rebound_called = False

    def forged_token(value, field):
        nonlocal rebound_called
        rebound_called = True
        return "laundered-attempt"

    monkeypatch.setattr(_batch_transport_module, "_token", forged_token)

    with pytest.raises(
        MarketBookBatchTransportError,
        match="attempt_id must be a non-empty canonical string",
    ):
        execute_market_book_batch_attempt(
            client,
            MarketBookAttemptHistory(plan, ()),
            batch_id=batch.batch_id,
            attempt_id=" bad-attempt ",
            required=True,
            request_id="sealed-attempt-token",
            scheduled_at=NOW,
            rate_gate=rate_gate,
            concurrency_gate=concurrency_gate,
        )

    assert rebound_called is False
    assert transport.calls == []


def test_attempt_executor_seals_pre_dispatch_batch_lookup(monkeypatch):
    plan = _plan(market_ids=("1.001",))
    batch = plan.batches[0]
    client, transport = _client(_payload(batch.market_ids))
    rate_gate, concurrency_gate = _gates()
    rebound_called = False

    def forged_canonical_batch(*args, **kwargs):
        nonlocal rebound_called
        rebound_called = True
        raise AssertionError("rebound executor batch lookup must not run")

    monkeypatch.setattr(
        _batch_transport_module,
        "_canonical_batch",
        forged_canonical_batch,
    )

    execution = execute_market_book_batch_attempt(
        client,
        MarketBookAttemptHistory(plan, ()),
        batch_id=batch.batch_id,
        attempt_id="sealed-executor-batch",
        required=True,
        request_id="sealed-executor-batch",
        scheduled_at=NOW,
        rate_gate=rate_gate,
        concurrency_gate=concurrency_gate,
    )

    assert rebound_called is False
    assert execution.outcome is MarketBookAttemptOutcome.EXACT_RESPONSE
    assert len(transport.calls) == 1


def test_attempt_executor_rejects_invalid_batch_before_transport_after_rebound(monkeypatch):
    plan = _plan(market_ids=("1.001",))
    client, transport = _client(_payload(("1.001",)))
    rate_gate, concurrency_gate = _gates()
    rebound_called = False

    def forged_canonical_batch(plan_arg, batch_id):
        nonlocal rebound_called
        rebound_called = True
        return plan_arg.batches[0]

    monkeypatch.setattr(
        _batch_transport_module,
        "_canonical_batch",
        forged_canonical_batch,
    )

    with pytest.raises(
        MarketBookBatchTransportError,
        match="batch_id must identify exactly one current canonical plan batch",
    ):
        execute_market_book_batch_attempt(
            client,
            MarketBookAttemptHistory(plan, ()),
            batch_id="forged-batch-id",
            attempt_id="sealed-invalid-batch",
            required=True,
            request_id="sealed-invalid-batch",
            scheduled_at=NOW,
            rate_gate=rate_gate,
            concurrency_gate=concurrency_gate,
        )

    assert rebound_called is False
    assert transport.calls == []

def test_attempt_executor_seals_admission_error_type(monkeypatch):
    plan = _plan(market_ids=("1.001",))
    batch = plan.batches[0]
    client, transport = _client(_payload(batch.market_ids))
    rate_gate, concurrency_gate = _gates()
    for _ in range(5):
        assert rate_gate.reserve(("1.001",), scheduled_at=NOW).allowed is True
    rebound_called = False

    class ReboundAdmissionError(Exception):
        def __init__(self, *args, **kwargs):
            nonlocal rebound_called
            rebound_called = True
            super().__init__("rebound admission error")

    monkeypatch.setattr(
        _batch_transport_module,
        "MarketBookBatchAdmissionError",
        ReboundAdmissionError,
    )

    execution = execute_market_book_batch_attempt(
        client,
        MarketBookAttemptHistory(plan, ()),
        batch_id=batch.batch_id,
        attempt_id="sealed-admission-type",
        required=True,
        request_id="sealed-admission-type",
        scheduled_at=NOW,
        rate_gate=rate_gate,
        concurrency_gate=concurrency_gate,
    )

    assert rebound_called is False
    assert execution.outcome is MarketBookAttemptOutcome.NOT_DISPATCHED_RATE
    assert execution.result is None
    assert execution.history.required_gap_attempt_ids == ("sealed-admission-type",)
    assert transport.calls == []


def test_attempt_executor_seals_transport_exception_aliases(monkeypatch):
    plan = _plan(market_ids=("1.001",))
    batch = plan.batches[0]

    class TimeoutTransport:
        def __init__(self):
            self.calls = 0

        def post(self, url, *, headers, body, timeout_seconds):
            self.calls += 1
            raise TimeoutError("forced transport timeout")

    transport = TimeoutTransport()
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-secret", "session-secret"),
        transport=transport,
        clock=lambda: NOW,
        venue_id="betfair-global",
        account_id="configured-account",
    )
    rate_gate, concurrency_gate = _gates()

    class ReboundTransportModule:
        BetfairMarketBookTransportError = LookupError
        BetfairMarketBookProviderError = LookupError
        BetfairMarketBookProtocolError = LookupError

    monkeypatch.setattr(
        _batch_transport_module,
        "_transport",
        ReboundTransportModule(),
    )

    execution = execute_market_book_batch_attempt(
        client,
        MarketBookAttemptHistory(plan, ()),
        batch_id=batch.batch_id,
        attempt_id="sealed-transport-exception-alias",
        required=True,
        request_id="sealed-transport-exception-alias",
        scheduled_at=NOW,
        rate_gate=rate_gate,
        concurrency_gate=concurrency_gate,
    )

    assert transport.calls == 1
    assert execution.outcome is MarketBookAttemptOutcome.TRANSPORT_FAILURE
    assert execution.result is None
    assert execution.history.required_gap_attempt_ids == (
        "sealed-transport-exception-alias",
    )


def test_attempt_executor_seals_completeness_error_type(monkeypatch):
    plan = _plan(market_ids=("1.001", "1.002"))
    batch = plan.batches[0]
    client, transport = _client(_payload(("1.001", "1.001")))
    rate_gate, concurrency_gate = _gates()
    rebound_called = False

    class ReboundCompletenessError(Exception):
        def __init__(self, *args, **kwargs):
            nonlocal rebound_called
            rebound_called = True
            super().__init__("rebound completeness error")

    monkeypatch.setattr(
        _batch_transport_module,
        "MarketBookCompletenessError",
        ReboundCompletenessError,
    )

    execution = execute_market_book_batch_attempt(
        client,
        MarketBookAttemptHistory(plan, ()),
        batch_id=batch.batch_id,
        attempt_id="sealed-completeness-error",
        required=True,
        request_id="sealed-completeness-error",
        scheduled_at=NOW,
        rate_gate=rate_gate,
        concurrency_gate=concurrency_gate,
    )

    assert rebound_called is False
    assert execution.outcome is MarketBookAttemptOutcome.PARSE_FAILURE
    assert execution.result is None
    assert execution.history.required_gap_attempt_ids == (
        "sealed-completeness-error",
    )
    assert len(transport.calls) == 1


def test_attempt_executor_seals_post_dispatch_failure_type(monkeypatch):
    plan = _plan(market_ids=("1.001",), order_projection="EXECUTABLE")
    batch = plan.batches[0]
    rate_gate, concurrency_gate = _gates()
    rebound_called = False

    class LeaseDroppingTransport:
        def __init__(self):
            self.calls = 0

        def post(self, url, *, headers, body, timeout_seconds):
            self.calls += 1
            with concurrency_gate._lock:
                concurrency_gate._active.clear()
            return _payload(batch.market_ids)

    transport = LeaseDroppingTransport()
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-secret", "session-secret"),
        transport=transport,
        clock=lambda: NOW,
        venue_id="betfair-global",
        account_id="configured-account",
    )

    class ReboundPostDispatchFailure(Exception):
        def __init__(self, *args, **kwargs):
            nonlocal rebound_called
            rebound_called = True
            super().__init__("rebound post-dispatch failure")

    monkeypatch.setattr(
        _batch_transport_module,
        "MarketBookPostDispatchFailure",
        ReboundPostDispatchFailure,
    )

    execution = execute_market_book_batch_attempt(
        client,
        MarketBookAttemptHistory(plan, ()),
        batch_id=batch.batch_id,
        attempt_id="sealed-post-dispatch-type",
        required=True,
        request_id="sealed-post-dispatch-type",
        scheduled_at=NOW,
        rate_gate=rate_gate,
        concurrency_gate=concurrency_gate,
    )

    assert rebound_called is False
    assert transport.calls == 1
    assert execution.outcome is MarketBookAttemptOutcome.TRANSPORT_FAILURE
    assert execution.result is None
    assert execution.history.required_gap_attempt_ids == (
        "sealed-post-dispatch-type",
    )

def test_response_append_seals_record_issuer_and_history_validator(monkeypatch):
    plan = _plan(market_ids=("1.001",))
    batch = plan.batches[0]
    client, _ = _client(_payload(batch.market_ids))
    issued = _read(client, plan, batch_id=batch.batch_id)
    history = MarketBookAttemptHistory(plan, ())
    issue_called = False
    validate_called = False

    def forged_issue(cls, *args, **kwargs):
        nonlocal issue_called
        issue_called = True
        raise AssertionError("rebound attempt record issuer must not run")

    def forged_history_validate(self):
        nonlocal validate_called
        validate_called = True
        raise AssertionError("rebound history validator must not run")

    monkeypatch.setattr(
        _batch_transport_module.MarketBookAttemptRecord,
        "issue",
        classmethod(forged_issue),
    )
    monkeypatch.setattr(
        MarketBookAttemptHistory,
        "__post_init__",
        forged_history_validate,
    )

    updated = append_market_book_transport_attempt(
        history,
        issued,
        attempt_id="sealed-response-history",
        required=True,
    )

    assert issue_called is False
    assert validate_called is False
    assert updated.records[-1].outcome is MarketBookAttemptOutcome.EXACT_RESPONSE
    assert updated.records[-1].exact_receipt == issued.receipt


def test_nonresponse_append_seals_record_issuer(monkeypatch):
    plan = _plan(market_ids=("1.001",))
    batch = plan.batches[0]
    history = MarketBookAttemptHistory(plan, ())
    client, transport = _client(_payload(batch.market_ids))
    rate_gate, concurrency_gate = _gates()
    for _ in range(5):
        assert rate_gate.reserve(("1.001",), scheduled_at=NOW).allowed is True
    issue_called = False

    def forged_issue(cls, *args, **kwargs):
        nonlocal issue_called
        issue_called = True
        raise AssertionError("rebound nonresponse record issuer must not run")

    monkeypatch.setattr(
        _batch_transport_module.MarketBookAttemptRecord,
        "issue",
        classmethod(forged_issue),
    )

    execution = execute_market_book_batch_attempt(
        client,
        history,
        batch_id=batch.batch_id,
        attempt_id="sealed-nonresponse-issuer",
        required=True,
        request_id="sealed-nonresponse-issuer",
        scheduled_at=NOW,
        rate_gate=rate_gate,
        concurrency_gate=concurrency_gate,
    )

    assert issue_called is False
    assert execution.outcome is MarketBookAttemptOutcome.NOT_DISPATCHED_RATE
    assert execution.history.required_gap_attempt_ids == (
        "sealed-nonresponse-issuer",
    )
    assert transport.calls == []


def test_bound_append_seals_result_class_rebind(monkeypatch):
    plan = _plan(market_ids=("1.001",))
    batch = plan.batches[0]
    client, _ = _client(_payload(batch.market_ids))
    issued = _read(client, plan, batch_id=batch.batch_id)
    history = MarketBookAttemptHistory(plan, ())

    class ReboundResult:
        pass

    monkeypatch.setattr(
        _batch_transport_module,
        "MarketBookBatchTransportResult",
        ReboundResult,
    )

    updated = append_market_book_transport_attempt(
        history,
        issued,
        attempt_id="sealed-result-class-append",
        required=True,
    )

    assert updated.records[-1].outcome is MarketBookAttemptOutcome.EXACT_RESPONSE
    assert updated.records[-1].exact_receipt == issued.receipt


def test_execution_validator_seals_outcome_enum_rebind(monkeypatch):
    plan = _plan(market_ids=("1.001",))
    batch = plan.batches[0]
    client, _ = _client(_payload(batch.market_ids))
    issued = _read(client, plan, batch_id=batch.batch_id)
    history = append_market_book_transport_attempt(
        MarketBookAttemptHistory(plan, ()),
        issued,
        attempt_id="sealed-enum-execution",
        required=True,
    )

    class ReboundOutcome:
        pass

    monkeypatch.setattr(
        _batch_transport_module,
        "MarketBookAttemptOutcome",
        ReboundOutcome,
    )

    execution = MarketBookBatchAttemptExecution(
        history,
        MarketBookAttemptOutcome.EXACT_RESPONSE,
        issued,
    )

    assert execution.outcome is MarketBookAttemptOutcome.EXACT_RESPONSE
    assert execution.result is issued


def test_execution_validator_seals_receipt_status_rebind(monkeypatch):
    plan = _plan(market_ids=("1.001",))
    batch = plan.batches[0]
    client, _ = _client(_payload(batch.market_ids))
    issued = _read(client, plan, batch_id=batch.batch_id)
    history = append_market_book_transport_attempt(
        MarketBookAttemptHistory(plan, ()),
        issued,
        attempt_id="sealed-status-execution",
        required=True,
    )

    class ReboundStatus:
        pass

    monkeypatch.setattr(
        _batch_transport_module,
        "BatchReceiptStatus",
        ReboundStatus,
    )

    issued.assert_issued()
    execution = MarketBookBatchAttemptExecution(
        history,
        MarketBookAttemptOutcome.EXACT_RESPONSE,
        issued,
    )

    assert execution.result is issued
    assert execution.history.records[-1].exact_receipt == issued.receipt


def test_execution_validator_seals_result_class_rebind(monkeypatch):
    plan = _plan(market_ids=("1.001",))
    batch = plan.batches[0]
    client, _ = _client(_payload(batch.market_ids))
    issued = _read(client, plan, batch_id=batch.batch_id)
    history = append_market_book_transport_attempt(
        MarketBookAttemptHistory(plan, ()),
        issued,
        attempt_id="sealed-result-class-execution",
        required=True,
    )

    class ReboundResult:
        pass

    monkeypatch.setattr(
        _batch_transport_module,
        "MarketBookBatchTransportResult",
        ReboundResult,
    )

    execution = MarketBookBatchAttemptExecution(
        history,
        MarketBookAttemptOutcome.EXACT_RESPONSE,
        issued,
    )

    assert execution.result is issued
    assert execution.outcome is MarketBookAttemptOutcome.EXACT_RESPONSE


def test_execution_validator_rejects_forged_copy_after_class_rebind(monkeypatch):
    plan = _plan(market_ids=("1.001",))
    batch = plan.batches[0]
    client, _ = _client(_payload(batch.market_ids))
    issued = _read(client, plan, batch_id=batch.batch_id)
    history = append_market_book_transport_attempt(
        MarketBookAttemptHistory(plan, ()),
        issued,
        attempt_id="sealed-forged-class-execution",
        required=True,
    )
    forged = replace(issued)

    class ReboundResult:
        pass

    monkeypatch.setattr(
        _batch_transport_module,
        "MarketBookBatchTransportResult",
        ReboundResult,
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

def test_attempt_executor_seals_plan_lifecycle_hooks(monkeypatch):
    plan = _plan(market_ids=("1.001",))
    batch = plan.batches[0]
    history = MarketBookAttemptHistory(plan, ())
    client, transport = _client(_payload(batch.market_ids))
    rate_gate, concurrency_gate = _gates()
    init_called = False
    post_called = False

    def forged_init(self, *args, **kwargs):
        nonlocal init_called
        init_called = True
        raise AssertionError("rebound plan init must not run")

    def forged_post_init(self):
        nonlocal post_called
        post_called = True
        raise AssertionError("rebound plan post-init must not run")

    monkeypatch.setattr(MarketBookReadPlan, "__init__", forged_init)
    monkeypatch.setattr(MarketBookReadPlan, "__post_init__", forged_post_init)

    execution = execute_market_book_batch_attempt(
        client,
        history,
        batch_id=batch.batch_id,
        attempt_id="sealed-plan-lifecycle",
        required=True,
        request_id="sealed-plan-lifecycle",
        scheduled_at=NOW,
        rate_gate=rate_gate,
        concurrency_gate=concurrency_gate,
    )

    assert init_called is False
    assert post_called is False
    assert execution.outcome is MarketBookAttemptOutcome.EXACT_RESPONSE
    assert len(transport.calls) == 1


def test_attempt_executor_seals_history_lifecycle_hooks(monkeypatch):
    plan = _plan(market_ids=("1.001",))
    batch = plan.batches[0]
    history = MarketBookAttemptHistory(plan, ())
    client, transport = _client(_payload(batch.market_ids))
    rate_gate, concurrency_gate = _gates()
    init_called = False
    post_called = False

    def forged_init(self, *args, **kwargs):
        nonlocal init_called
        init_called = True
        raise AssertionError("rebound history init must not run")

    def forged_post_init(self):
        nonlocal post_called
        post_called = True
        raise AssertionError("rebound history post-init must not run")

    monkeypatch.setattr(MarketBookAttemptHistory, "__init__", forged_init)
    monkeypatch.setattr(MarketBookAttemptHistory, "__post_init__", forged_post_init)

    execution = execute_market_book_batch_attempt(
        client,
        history,
        batch_id=batch.batch_id,
        attempt_id="sealed-history-lifecycle",
        required=True,
        request_id="sealed-history-lifecycle",
        scheduled_at=NOW,
        rate_gate=rate_gate,
        concurrency_gate=concurrency_gate,
    )

    assert init_called is False
    assert post_called is False
    assert execution.outcome is MarketBookAttemptOutcome.EXACT_RESPONSE
    assert len(transport.calls) == 1


def test_attempt_executor_seals_lifecycle_hooks_on_nonresponse(monkeypatch):
    plan = _plan(market_ids=("1.001",))
    batch = plan.batches[0]
    history = MarketBookAttemptHistory(plan, ())
    client, transport = _client(_payload(batch.market_ids))
    rate_gate, concurrency_gate = _gates()
    for _ in range(5):
        assert rate_gate.reserve(("1.001",), scheduled_at=NOW).allowed is True

    monkeypatch.setattr(
        MarketBookReadPlan,
        "__post_init__",
        lambda self: (_ for _ in ()).throw(
            AssertionError("rebound plan post-init must not run")
        ),
    )
    monkeypatch.setattr(
        MarketBookAttemptHistory,
        "__post_init__",
        lambda self: (_ for _ in ()).throw(
            AssertionError("rebound history post-init must not run")
        ),
    )

    execution = execute_market_book_batch_attempt(
        client,
        history,
        batch_id=batch.batch_id,
        attempt_id="sealed-lifecycle-nonresponse",
        required=True,
        request_id="sealed-lifecycle-nonresponse",
        scheduled_at=NOW,
        rate_gate=rate_gate,
        concurrency_gate=concurrency_gate,
    )

    assert execution.outcome is MarketBookAttemptOutcome.NOT_DISPATCHED_RATE
    assert execution.history.required_gap_attempt_ids == (
        "sealed-lifecycle-nonresponse",
    )
    assert transport.calls == []

def test_cleanup_seals_nested_transport_clock_rebind(monkeypatch):
    plan = _plan(market_ids=("1.001",), order_projection="EXECUTABLE")
    batch = plan.batches[0]
    client, transport = _client(_payload(batch.market_ids))
    rate_gate, concurrency_gate = _gates()
    rebound_called = False

    def forged_transport_now(*args, **kwargs):
        nonlocal rebound_called
        rebound_called = True
        raise AssertionError("rebound cleanup clock must not run")

    monkeypatch.setattr(
        _batch_transport_module,
        "_transport_now",
        forged_transport_now,
    )

    result = read_market_book_batch(
        client,
        plan,
        batch_id=batch.batch_id,
        request_id="sealed-cleanup-clock",
        scheduled_at=NOW,
        rate_gate=rate_gate,
        concurrency_gate=concurrency_gate,
    )

    result.assert_issued()
    assert rebound_called is False
    assert concurrency_gate.snapshot().active == ()
    assert len(transport.calls) == 1


def test_failure_cleanup_seals_nested_release_rebind(monkeypatch):
    plan = _plan(market_ids=("1.001",), order_projection="EXECUTABLE")
    batch = plan.batches[0]
    client, transport = _client(_payload(batch.market_ids))
    rate_gate, concurrency_gate = _gates()
    for _ in range(5):
        assert rate_gate.reserve(("1.001",), scheduled_at=NOW).allowed is True
    rebound_called = False

    def forged_release(*args, **kwargs):
        nonlocal rebound_called
        rebound_called = True
        raise AssertionError("rebound nested release must not run")

    monkeypatch.setattr(
        _batch_transport_module,
        "_release_projection_lease",
        forged_release,
    )

    with pytest.raises(MarketBookBatchAdmissionError) as exc_info:
        read_market_book_batch(
            client,
            plan,
            batch_id=batch.batch_id,
            request_id="sealed-nested-failure-release",
            scheduled_at=NOW,
            rate_gate=rate_gate,
            concurrency_gate=concurrency_gate,
        )

    assert exc_info.value.outcome is MarketBookAttemptOutcome.NOT_DISPATCHED_RATE
    assert rebound_called is False
    assert concurrency_gate.snapshot().active == ()
    assert transport.calls == []




@pytest.mark.parametrize(
    ("attribute", "replacement"),
    (
        ("_normalize_market_ids", lambda *args, **kwargs: ("9.999",)),
        ("_utc_microseconds", lambda *args, **kwargs: 0),
        ("MarketBookRateDecision", object),
        ("_MAX_CALLS_PER_WINDOW", 999),
        ("_WINDOW_MICROSECONDS", 1),
    ),
)
def test_transport_rejects_rebound_rate_gate_primitives_before_mutation(
    monkeypatch,
    attribute,
    replacement,
):
    plan = _plan(market_ids=("1.001",))
    batch = plan.batches[0]
    client, transport = _client(_payload(batch.market_ids))
    rate_gate, concurrency_gate = _gates()
    rate_before = rate_gate.snapshot()

    monkeypatch.setattr(_rate_gate_module, attribute, replacement)

    with pytest.raises(MarketBookBatchAdmissionError) as exc_info:
        read_market_book_batch(
            client,
            plan,
            batch_id=batch.batch_id,
            request_id=f"rebound-rate-{attribute}",
            scheduled_at=NOW,
            rate_gate=rate_gate,
            concurrency_gate=concurrency_gate,
        )

    assert exc_info.value.outcome is MarketBookAttemptOutcome.NOT_DISPATCHED_RATE
    assert rate_gate.snapshot() == rate_before
    assert transport.calls == []


@pytest.mark.parametrize(
    ("attribute", "replacement"),
    (
        ("_validate_request_id", lambda *args, **kwargs: "forged-request"),
        ("_utc_microseconds", lambda *args, **kwargs: 0),
        ("MarketBookProjectionConcurrencyDecision", object),
        ("MarketBookProjectionLease", object),
        ("_MAX_LOCAL_PROJECTION_REQUESTS_UNRESOLVED", 999),
    ),
)
def test_transport_rejects_rebound_projection_gate_primitives_before_mutation(
    monkeypatch,
    attribute,
    replacement,
):
    plan = _plan(market_ids=("1.001",), order_projection="EXECUTABLE")
    batch = plan.batches[0]
    client, transport = _client(_payload(batch.market_ids))
    rate_gate, concurrency_gate = _gates()
    concurrency_before = concurrency_gate.snapshot()
    rate_before = rate_gate.snapshot()

    monkeypatch.setattr(_projection_gate_module, attribute, replacement)

    with pytest.raises(MarketBookBatchAdmissionError) as exc_info:
        read_market_book_batch(
            client,
            plan,
            batch_id=batch.batch_id,
            request_id=f"rebound-projection-{attribute}",
            scheduled_at=NOW,
            rate_gate=rate_gate,
            concurrency_gate=concurrency_gate,
        )

    assert exc_info.value.outcome is MarketBookAttemptOutcome.NOT_DISPATCHED_CONCURRENCY
    assert concurrency_gate.snapshot() == concurrency_before
    assert rate_gate.snapshot() == rate_before
    assert transport.calls == []


def test_transport_rejects_rate_decision_lifecycle_tamper_before_mutation(
    monkeypatch,
):
    plan = _plan(market_ids=("1.001",))
    batch = plan.batches[0]
    client, transport = _client(_payload(batch.market_ids))
    rate_gate, concurrency_gate = _gates()
    rate_before = rate_gate.snapshot()
    decision_type = _rate_gate_module.MarketBookRateDecision
    original_init = decision_type.__init__

    def forged_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        object.__setattr__(self, "market_ids", ("9.999",))

    monkeypatch.setattr(decision_type, "__init__", forged_init)

    with pytest.raises(MarketBookBatchAdmissionError) as exc_info:
        read_market_book_batch(
            client,
            plan,
            batch_id=batch.batch_id,
            request_id="forged-rate-decision-binding",
            scheduled_at=NOW,
            rate_gate=rate_gate,
            concurrency_gate=concurrency_gate,
        )

    assert exc_info.value.outcome is MarketBookAttemptOutcome.NOT_DISPATCHED_RATE
    assert rate_gate.snapshot() == rate_before
    assert transport.calls == []


def test_transport_rejects_projection_decision_lifecycle_tamper_before_lease(
    monkeypatch,
):
    plan = _plan(market_ids=("1.001",), order_projection="EXECUTABLE")
    batch = plan.batches[0]
    client, transport = _client(_payload(batch.market_ids))
    rate_gate, concurrency_gate = _gates()
    concurrency_before = concurrency_gate.snapshot()
    decision_type = _projection_gate_module.MarketBookProjectionConcurrencyDecision
    original_init = decision_type.__init__

    def forged_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        object.__setattr__(
            self,
            "observed_at_utc_us",
            self.observed_at_utc_us + 1,
        )

    monkeypatch.setattr(decision_type, "__init__", forged_init)

    with pytest.raises(MarketBookBatchAdmissionError) as exc_info:
        read_market_book_batch(
            client,
            plan,
            batch_id=batch.batch_id,
            request_id="forged-projection-decision-time",
            scheduled_at=NOW,
            rate_gate=rate_gate,
            concurrency_gate=concurrency_gate,
        )

    assert (
        exc_info.value.outcome
        is MarketBookAttemptOutcome.NOT_DISPATCHED_CONCURRENCY
    )
    assert concurrency_gate.snapshot() == concurrency_before
    assert rate_gate.snapshot().markets == ()
    assert transport.calls == []


@pytest.mark.parametrize(
    ("module", "class_name"),
    (
        (_rate_gate_module, "MarketBookRateDecision"),
        (_projection_gate_module, "MarketBookProjectionConcurrencyDecision"),
        (_projection_gate_module, "MarketBookProjectionLease"),
    ),
)
def test_transport_rejects_gate_post_init_rebind_before_admission(
    monkeypatch,
    module,
    class_name,
):
    plan = _plan(market_ids=("1.001",), order_projection="EXECUTABLE")
    batch = plan.batches[0]
    client, transport = _client(_payload(batch.market_ids))
    rate_gate, concurrency_gate = _gates()
    rate_before = rate_gate.snapshot()
    concurrency_before = concurrency_gate.snapshot()
    target = getattr(module, class_name)

    monkeypatch.setattr(
        target,
        "__post_init__",
        lambda self: None,
    )

    expected = (
        MarketBookAttemptOutcome.NOT_DISPATCHED_RATE
        if module is _rate_gate_module
        else MarketBookAttemptOutcome.NOT_DISPATCHED_CONCURRENCY
    )
    with pytest.raises(MarketBookBatchAdmissionError) as exc_info:
        read_market_book_batch(
            client,
            plan,
            batch_id=batch.batch_id,
            request_id=f"rebound-post-init-{class_name}",
            scheduled_at=NOW,
            rate_gate=rate_gate,
            concurrency_gate=concurrency_gate,
        )

    assert exc_info.value.outcome is expected
    assert rate_gate.snapshot() == rate_before
    if module is _projection_gate_module:
        assert concurrency_gate.snapshot() == concurrency_before
    else:
        # Projection admission precedes rate admission. A rate preflight failure
        # releases any acquired lease but does not rewind canonical causal time.
        assert concurrency_gate.snapshot().active == ()
    assert transport.calls == []

def test_attempt_executor_records_result_authority_finalization_failure(monkeypatch):
    plan = _plan(market_ids=("1.001",))
    batch = plan.batches[0]
    history = MarketBookAttemptHistory(plan, ())
    client, transport = _client(_payload(batch.market_ids))
    rate_gate, concurrency_gate = _gates()
    result_type = _batch_transport_module.MarketBookBatchTransportResult
    original_init = result_type.__init__

    def forged_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        object.__setattr__(self, "plan_id", "forged")

    monkeypatch.setattr(result_type, "__post_init__", lambda self: None)
    monkeypatch.setattr(result_type, "__init__", forged_init)
    monkeypatch.setattr(
        _batch_transport_module,
        "_sha256_token",
        lambda value, field: value,
    )

    execution = execute_market_book_batch_attempt(
        client,
        history,
        batch_id=batch.batch_id,
        attempt_id="result-finalization-failure",
        required=True,
        request_id="result-finalization-failure",
        scheduled_at=NOW,
        rate_gate=rate_gate,
        concurrency_gate=concurrency_gate,
    )

    assert execution.outcome is MarketBookAttemptOutcome.TRANSPORT_FAILURE
    assert execution.result is None
    assert execution.history.records[-1].outcome is MarketBookAttemptOutcome.TRANSPORT_FAILURE
    assert len(transport.calls) == 1


def test_issued_result_uses_canonical_error_after_module_error_rebind(monkeypatch):
    plan = _plan(market_ids=("1.001",))
    batch = plan.batches[0]
    client, transport = _client(_payload(batch.market_ids))
    rate_gate, concurrency_gate = _gates()

    result = read_market_book_batch(
        client,
        plan,
        batch_id=batch.batch_id,
        request_id="sealed-result-error-type",
        scheduled_at=NOW,
        rate_gate=rate_gate,
        concurrency_gate=concurrency_gate,
    )
    object.__setattr__(result, "plan_id", "0" * 64)
    monkeypatch.setattr(
        _batch_transport_module,
        "MarketBookBatchTransportError",
        RuntimeError,
    )

    with pytest.raises(MarketBookBatchTransportError):
        result.assert_issued()

    assert len(transport.calls) == 1


def test_attempt_executor_seals_history_type_global_rebind(monkeypatch):
    plan = _plan(market_ids=("1.001",))
    batch = plan.batches[0]
    history = MarketBookAttemptHistory(plan, ())
    client, transport = _client(_payload(batch.market_ids))
    rate_gate, concurrency_gate = _gates()

    monkeypatch.setattr(
        _batch_transport_module,
        "MarketBookAttemptHistory",
        object,
    )

    execution = execute_market_book_batch_attempt(
        client,
        history,
        batch_id=batch.batch_id,
        attempt_id="sealed-history-global",
        required=True,
        request_id="sealed-history-global",
        scheduled_at=NOW,
        rate_gate=rate_gate,
        concurrency_gate=concurrency_gate,
    )

    assert execution.outcome is MarketBookAttemptOutcome.EXACT_RESPONSE
    assert len(transport.calls) == 1


def test_attempt_executor_uses_canonical_duplicate_error_after_rebind(monkeypatch):
    plan = _plan(market_ids=("1.001",))
    batch = plan.batches[0]
    history = MarketBookAttemptHistory(plan, ())
    client, transport = _client(_payload(batch.market_ids))
    rate_gate, concurrency_gate = _gates()

    first = execute_market_book_batch_attempt(
        client,
        history,
        batch_id=batch.batch_id,
        attempt_id="duplicate-sealed-error",
        required=True,
        request_id="duplicate-sealed-error-1",
        scheduled_at=NOW,
        rate_gate=rate_gate,
        concurrency_gate=concurrency_gate,
    )
    monkeypatch.setattr(
        _batch_transport_module,
        "MarketBookBatchTransportError",
        RuntimeError,
    )

    with pytest.raises(MarketBookBatchTransportError):
        execute_market_book_batch_attempt(
            client,
            first.history,
            batch_id=batch.batch_id,
            attempt_id="duplicate-sealed-error",
            required=True,
            request_id="duplicate-sealed-error-2",
            scheduled_at=NOW,
            rate_gate=rate_gate,
            concurrency_gate=concurrency_gate,
        )

    assert len(transport.calls) == 1

def test_attempt_executor_rejects_rebound_receipt_hash_helpers(monkeypatch):
    plan = _plan(market_ids=("1.001",))
    batch = plan.batches[0]
    history = MarketBookAttemptHistory(plan, ())
    client, transport = _client(_payload(batch.market_ids))
    rate_gate, concurrency_gate = _gates()

    monkeypatch.setattr(
        _batch_completeness_module,
        "_sha",
        lambda value: "0" * 64,
    )
    monkeypatch.setattr(
        _batch_completeness_module,
        "_receipt_id",
        lambda payload: "1" * 64,
    )

    execution = execute_market_book_batch_attempt(
        client,
        history,
        batch_id=batch.batch_id,
        attempt_id="sealed-receipt-hash",
        required=True,
        request_id="sealed-receipt-hash",
        scheduled_at=NOW,
        rate_gate=rate_gate,
        concurrency_gate=concurrency_gate,
    )

    assert execution.outcome is MarketBookAttemptOutcome.PARSE_FAILURE
    assert execution.result is None
    assert execution.history.records[-1].outcome is MarketBookAttemptOutcome.PARSE_FAILURE
    assert len(transport.calls) == 1


def test_attempt_executor_rejects_rebound_receipt_market_parser(monkeypatch):
    plan = _plan(market_ids=("1.001",))
    batch = plan.batches[0]
    history = MarketBookAttemptHistory(plan, ())
    client, transport = _client(_payload(batch.market_ids))
    rate_gate, concurrency_gate = _gates()

    monkeypatch.setattr(
        _batch_completeness_module,
        "_market_ids_from_response",
        lambda response: ("9.999",),
    )

    execution = execute_market_book_batch_attempt(
        client,
        history,
        batch_id=batch.batch_id,
        attempt_id="sealed-receipt-market-parser",
        required=True,
        request_id="sealed-receipt-market-parser",
        scheduled_at=NOW,
        rate_gate=rate_gate,
        concurrency_gate=concurrency_gate,
    )

    assert execution.outcome is MarketBookAttemptOutcome.PARSE_FAILURE
    assert execution.result is None
    assert execution.history.records[-1].outcome is MarketBookAttemptOutcome.PARSE_FAILURE
    assert len(transport.calls) == 1
