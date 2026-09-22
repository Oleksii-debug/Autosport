import pytest

from autosport.betfair_marketbook_batch_completeness import (
    BatchReceiptStatus,
    MarketBookBatchReceipt,
    MarketBookCompletenessError,
    MarketBookReadCompleteness,
    ReadCompletenessStatus,
)
from autosport.betfair_marketbook_batch_plan import MarketBookReadPlan


def mids(count: int) -> tuple[str, ...]:
    return tuple(f"1.{index:09d}" for index in range(count))


def response(ids):
    return [{"marketId": mid, "status": "OPEN", "runners": []} for mid in ids]


def two_batch_plan() -> MarketBookReadPlan:
    return MarketBookReadPlan(mids(12), "OPEN", ("EX_ALL_OFFERS",))


def test_exact_receipts_for_every_batch_prove_structural_completeness():
    plan = two_batch_plan()
    receipts = tuple(MarketBookBatchReceipt.from_response(batch, response(batch.market_ids)) for batch in plan.batches)
    proof = MarketBookReadCompleteness(plan, receipts)
    assert proof.status is ReadCompletenessStatus.COMPLETE
    assert proof.pending_batch_ids == ()
    assert proof.missing_receipt_batch_ids == ()
    assert proof.evidence_payload["structural_response_coverage_complete"] is True
    assert proof.evidence_payload["provider_observation_authenticated"] is False
    assert proof.evidence_payload["provider_freshness_proven"] is False


def test_empty_or_partial_success_cannot_become_complete_market_truth():
    plan = two_batch_plan()
    first, second = plan.batches
    partial = MarketBookBatchReceipt.from_response(first, response(first.market_ids[:-1]))
    exact = MarketBookBatchReceipt.from_response(second, response(second.market_ids))
    proof = MarketBookReadCompleteness(plan, (partial, exact))
    assert partial.status is BatchReceiptStatus.INCOMPLETE_RESPONSE
    assert partial.missing_market_ids == (first.market_ids[-1],)
    assert proof.status is ReadCompletenessStatus.DEGRADED
    assert proof.pending_batch_ids == (first.batch_id,)

    empty = MarketBookBatchReceipt.from_response(first, [])
    assert empty.status is BatchReceiptStatus.INCOMPLETE_RESPONSE
    assert empty.missing_market_ids == first.market_ids


def test_unexpected_provider_market_is_explicitly_degraded():
    plan = two_batch_plan()
    first = plan.batches[0]
    receipt = MarketBookBatchReceipt.from_response(first, response((*first.market_ids, "1.unexpected")))
    assert receipt.status is BatchReceiptStatus.INCOMPLETE_RESPONSE
    assert receipt.unexpected_market_ids == ("1.unexpected",)
    assert MarketBookReadCompleteness(plan, (receipt,)).status is ReadCompletenessStatus.DEGRADED


def test_provider_transport_or_parse_failure_is_not_empty_success():
    plan = two_batch_plan()
    first = plan.batches[0]
    receipt = MarketBookBatchReceipt.from_failure(
        first,
        failure_kind="PROVIDER_ERROR",
        failure_code="SERVICE_BUSY",
        detail_payload={"error": "SERVICE_BUSY"},
    )
    assert receipt.status is BatchReceiptStatus.EXPLICIT_FAILURE
    assert receipt.observed_market_ids == ()
    assert receipt.missing_market_ids == first.market_ids
    proof = MarketBookReadCompleteness(plan, (receipt,))
    assert proof.status is ReadCompletenessStatus.DEGRADED
    assert first.batch_id in proof.pending_batch_ids


def test_missing_batch_receipt_remains_pending_in_plan_order():
    plan = two_batch_plan()
    second = plan.batches[1]
    proof = MarketBookReadCompleteness(
        plan, (MarketBookBatchReceipt.from_response(second, response(second.market_ids)),)
    )
    assert proof.missing_receipt_batch_ids == (plan.batches[0].batch_id,)
    assert proof.pending_batch_ids == (plan.batches[0].batch_id,)


def test_receipt_input_order_does_not_change_aggregate_identity():
    plan = two_batch_plan()
    a, b = [MarketBookBatchReceipt.from_response(batch, response(batch.market_ids)) for batch in plan.batches]
    left = MarketBookReadCompleteness(plan, (a, b))
    right = MarketBookReadCompleteness(plan, (b, a))
    assert left.receipts == right.receipts
    assert left.evidence_id == right.evidence_id


def test_duplicate_or_unknown_batch_receipts_fail_closed():
    plan = two_batch_plan()
    receipt = MarketBookBatchReceipt.from_response(plan.batches[0], response(plan.batches[0].market_ids))
    with pytest.raises(MarketBookCompletenessError, match="duplicate receipt"):
        MarketBookReadCompleteness(plan, (receipt, receipt))

    foreign_plan = MarketBookReadPlan(("9.1",), "OPEN")
    foreign = MarketBookBatchReceipt.from_response(foreign_plan.batches[0], response(("9.1",)))
    with pytest.raises(MarketBookCompletenessError, match="unknown batch_id"):
        MarketBookReadCompleteness(plan, (foreign,))


def test_malformed_provider_response_fails_closed_before_receipt_creation():
    batch = two_batch_plan().batches[0]
    with pytest.raises(MarketBookCompletenessError, match="sequence"):
        MarketBookBatchReceipt.from_response(batch, {"marketId": batch.market_ids[0]})
    with pytest.raises(MarketBookCompletenessError, match="marketId"):
        MarketBookBatchReceipt.from_response(batch, [{"status": "OPEN"}])
    with pytest.raises(MarketBookCompletenessError, match="duplicate marketId"):
        MarketBookBatchReceipt.from_response(batch, response((batch.market_ids[0], batch.market_ids[0])))


def test_tampered_receipt_cannot_rebind_complete_truth():
    plan = two_batch_plan()
    batch = plan.batches[0]
    receipt = MarketBookBatchReceipt.from_response(batch, response(batch.market_ids[:-1]))
    object.__setattr__(receipt, "status", BatchReceiptStatus.EXACT_RESPONSE)
    with pytest.raises(MarketBookCompletenessError, match="contradictory"):
        MarketBookReadCompleteness(plan, (receipt,))


def test_payload_digest_and_receipt_identity_are_content_sensitive():
    batch = two_batch_plan().batches[0]
    a = MarketBookBatchReceipt.from_response(batch, response(batch.market_ids))
    richer = response(batch.market_ids)
    richer[0]["totalMatched"] = 1.0
    b = MarketBookBatchReceipt.from_response(batch, richer)
    assert a.observed_market_ids == b.observed_market_ids
    assert a.payload_sha256 != b.payload_sha256
    assert a.receipt_id != b.receipt_id


def test_nonfinite_provider_payload_fails_closed_before_evidence_hashing():
    batch = two_batch_plan().batches[0]
    bad = response(batch.market_ids)
    bad[0]["totalMatched"] = float("nan")
    with pytest.raises(MarketBookCompletenessError, match="canonical JSON"):
        MarketBookBatchReceipt.from_response(batch, bad)


def test_completeness_proof_never_grants_dispatch_or_execution_authority():
    plan = MarketBookReadPlan(("1.1",), "OPEN")
    receipt = MarketBookBatchReceipt.from_response(plan.batches[0], response(("1.1",)))
    proof = MarketBookReadCompleteness(plan, (receipt,))
    assert proof.status is ReadCompletenessStatus.COMPLETE
    assert proof.evidence_payload["provider_dispatch_authorized"] is False
    assert proof.evidence_payload["execution_authorized"] is False
