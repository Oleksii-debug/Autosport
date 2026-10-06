from dataclasses import replace
from datetime import datetime, timezone
import json

import pytest

from autosport.betfair_account_readonly import (
    BETTING_JSON_RPC_ENDPOINT,
    BetfairReadOnlyClient,
    BetfairSessionCredentials,
)
from autosport.betfair_marketbook_batch_completeness import BatchReceiptStatus
from autosport.betfair_marketbook_batch_plan import MarketBookReadPlan
from autosport.betfair_marketbook_batch_transport import (
    MarketBookBatchTransportError,
    MarketBookBatchTransportResult,
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

    result = read_market_book_batch(client, plan, batch_id=batch.batch_id)

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

    result = read_market_book_batch(client, plan, batch_id=batch.batch_id)

    result.assert_issued()
    assert result.receipt.status is BatchReceiptStatus.INCOMPLETE_RESPONSE
    assert result.receipt.missing_market_ids == ("1.002",)
    assert result.structural_exact_response is False


def test_forged_copy_cannot_inherit_transport_issuance():
    plan = _plan()
    batch = plan.batches[0]
    client, _ = _client(_payload(batch.market_ids))
    issued = read_market_book_batch(client, plan, batch_id=batch.batch_id)

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
        read_market_book_batch(client, plan, batch_id=old_batch_id)

    assert transport.calls == []


def test_large_plan_uses_budget_partition_and_dispatches_only_named_batch():
    market_ids = tuple(f"1.{index:03d}" for index in range(41))
    plan = _plan(market_ids=market_ids, price_data=("EX_BEST_OFFERS",))
    assert len(plan.batches) == 2
    assert len(plan.batches[0].market_ids) == 40
    assert len(plan.batches[1].market_ids) == 1
    second = plan.batches[1]
    client, transport = _client(_payload(second.market_ids))

    result = read_market_book_batch(client, plan, batch_id=second.batch_id)

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
        read_market_book_batch(client, plan, batch_id=batch.batch_id)


def test_direct_transport_result_construction_cannot_mint_issuance():
    plan = _plan()
    batch = plan.batches[0]
    fake_receipt_client, _ = _client(_payload(batch.market_ids))
    issued = read_market_book_batch(
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
