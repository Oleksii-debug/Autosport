import json

import pytest

from autosport.betfair_marketbook_attempt_history import (
    MarketBookAttemptHistory,
    MarketBookAttemptHistoryError,
    MarketBookAttemptOutcome,
    MarketBookAttemptRecord,
)
from autosport.betfair_marketbook_batch_completeness import (
    BatchReceiptStatus,
    MarketBookBatchReceipt,
)
from autosport.betfair_marketbook_batch_plan import MarketBookReadPlan


def mids(count: int) -> tuple[str, ...]:
    return tuple(f"1.{index:09d}" for index in range(count))


def response(ids):
    return [{"marketId": mid, "status": "OPEN", "runners": []} for mid in ids]


def two_batch_plan() -> MarketBookReadPlan:
    return MarketBookReadPlan(mids(12), "OPEN", ("EX_ALL_OFFERS",))


def exact_receipt(plan: MarketBookReadPlan, batch_index: int) -> MarketBookBatchReceipt:
    batch = plan.batches[batch_index]
    return MarketBookBatchReceipt.from_response(batch, response(batch.market_ids))


def issue(
    plan: MarketBookReadPlan,
    *,
    attempt_id: str,
    sequence: int,
    batch_index: int,
    required: bool,
    outcome: MarketBookAttemptOutcome,
    previous=None,
    receipt=None,
) -> MarketBookAttemptRecord:
    batch = plan.batches[batch_index]
    return MarketBookAttemptRecord.issue(
        plan,
        attempt_id=attempt_id,
        sequence=sequence,
        batch_id=batch.batch_id,
        required=required,
        outcome=outcome,
        exact_receipt=receipt,
        previous_record=previous,
    )


def test_exact_required_attempts_for_every_batch_are_current_and_gap_free():
    plan = two_batch_plan()
    first = issue(
        plan,
        attempt_id="a1",
        sequence=1,
        batch_index=0,
        required=True,
        outcome=MarketBookAttemptOutcome.EXACT_RESPONSE,
        receipt=exact_receipt(plan, 0),
    )
    second = issue(
        plan,
        attempt_id="a2",
        sequence=2,
        batch_index=1,
        required=True,
        outcome=MarketBookAttemptOutcome.EXACT_RESPONSE,
        receipt=exact_receipt(plan, 1),
        previous=first,
    )
    history = MarketBookAttemptHistory(plan, (first, second))
    assert history.current_structural_complete is True
    assert history.current_pending_batch_ids == ()
    assert history.historical_required_gap_free is True
    assert history.required_gap_attempt_ids == ()


def test_required_failure_then_exact_retry_recovers_current_not_history():
    plan = MarketBookReadPlan(("1.1",), "OPEN")
    failed = issue(
        plan,
        attempt_id="failed",
        sequence=1,
        batch_index=0,
        required=True,
        outcome=MarketBookAttemptOutcome.TRANSPORT_FAILURE,
    )
    recovered = issue(
        plan,
        attempt_id="retry",
        sequence=2,
        batch_index=0,
        required=True,
        outcome=MarketBookAttemptOutcome.EXACT_RESPONSE,
        receipt=exact_receipt(plan, 0),
        previous=failed,
    )
    history = MarketBookAttemptHistory(plan, (failed, recovered))
    assert history.current_structural_complete is True
    assert history.historical_required_gap_free is False
    assert history.required_gap_attempt_ids == ("failed",)
    assert history.evidence_payload["required_gap_attempt_ids"] == ["failed"]


@pytest.mark.parametrize(
    "outcome",
    [
        MarketBookAttemptOutcome.INCOMPLETE_RESPONSE,
        MarketBookAttemptOutcome.PROVIDER_FAILURE,
        MarketBookAttemptOutcome.TRANSPORT_FAILURE,
        MarketBookAttemptOutcome.PARSE_FAILURE,
        MarketBookAttemptOutcome.NOT_DISPATCHED_RATE,
        MarketBookAttemptOutcome.NOT_DISPATCHED_CONCURRENCY,
        MarketBookAttemptOutcome.CRASH_PENDING,
    ],
)
def test_every_required_nonexact_outcome_is_an_irreversible_historical_gap(outcome):
    plan = MarketBookReadPlan(("1.1",), "OPEN")
    gap = issue(
        plan,
        attempt_id="gap",
        sequence=1,
        batch_index=0,
        required=True,
        outcome=outcome,
    )
    recovered = issue(
        plan,
        attempt_id="recovered",
        sequence=2,
        batch_index=0,
        required=True,
        outcome=MarketBookAttemptOutcome.EXACT_RESPONSE,
        receipt=exact_receipt(plan, 0),
        previous=gap,
    )
    history = MarketBookAttemptHistory(plan, (gap, recovered))
    assert history.current_structural_complete is True
    assert history.historical_required_gap_free is False
    assert history.required_gap_attempt_ids == ("gap",)


def test_latest_failure_after_prior_exact_makes_current_structure_degraded():
    plan = MarketBookReadPlan(("1.1",), "OPEN")
    exact = issue(
        plan,
        attempt_id="exact",
        sequence=1,
        batch_index=0,
        required=True,
        outcome=MarketBookAttemptOutcome.EXACT_RESPONSE,
        receipt=exact_receipt(plan, 0),
    )
    later = issue(
        plan,
        attempt_id="later-failure",
        sequence=2,
        batch_index=0,
        required=False,
        outcome=MarketBookAttemptOutcome.PROVIDER_FAILURE,
        previous=exact,
    )
    history = MarketBookAttemptHistory(plan, (exact, later))
    assert history.current_structural_complete is False
    assert history.current_pending_batch_ids == (plan.batches[0].batch_id,)
    assert history.historical_required_gap_free is True


def test_nonrequired_gap_does_not_rewrite_required_campaign_history():
    plan = MarketBookReadPlan(("1.1",), "OPEN")
    optional = issue(
        plan,
        attempt_id="optional",
        sequence=1,
        batch_index=0,
        required=False,
        outcome=MarketBookAttemptOutcome.NOT_DISPATCHED_RATE,
    )
    history = MarketBookAttemptHistory(plan, (optional,))
    assert history.required_gap_attempt_ids == ()
    assert history.historical_required_gap_free is True
    assert history.current_structural_complete is False


def test_exact_response_requires_a_valid_exact_receipt_for_the_same_batch():
    plan = two_batch_plan()
    with pytest.raises(MarketBookAttemptHistoryError, match="requires exact_receipt"):
        issue(
            plan,
            attempt_id="missing",
            sequence=1,
            batch_index=0,
            required=True,
            outcome=MarketBookAttemptOutcome.EXACT_RESPONSE,
        )

    foreign = exact_receipt(plan, 1)
    with pytest.raises(MarketBookAttemptHistoryError, match="another planned batch"):
        issue(
            plan,
            attempt_id="wrong-batch",
            sequence=1,
            batch_index=0,
            required=True,
            outcome=MarketBookAttemptOutcome.EXACT_RESPONSE,
            receipt=foreign,
        )

    batch = plan.batches[0]
    incomplete = MarketBookBatchReceipt.from_response(batch, response(batch.market_ids[:-1]))
    assert incomplete.status is BatchReceiptStatus.INCOMPLETE_RESPONSE
    with pytest.raises(MarketBookAttemptHistoryError, match="EXACT_RESPONSE status"):
        issue(
            plan,
            attempt_id="partial",
            sequence=1,
            batch_index=0,
            required=True,
            outcome=MarketBookAttemptOutcome.EXACT_RESPONSE,
            receipt=incomplete,
        )


def test_nonexact_outcomes_cannot_smuggle_a_positive_receipt():
    plan = MarketBookReadPlan(("1.1",), "OPEN")
    with pytest.raises(MarketBookAttemptHistoryError, match="only EXACT_RESPONSE"):
        issue(
            plan,
            attempt_id="smuggle",
            sequence=1,
            batch_index=0,
            required=True,
            outcome=MarketBookAttemptOutcome.PROVIDER_FAILURE,
            receipt=exact_receipt(plan, 0),
        )


def test_unknown_batch_and_duplicate_attempt_identity_fail_closed():
    plan = MarketBookReadPlan(("1.1",), "OPEN")
    with pytest.raises(MarketBookAttemptHistoryError, match="unknown planned batch"):
        MarketBookAttemptRecord.issue(
            plan,
            attempt_id="foreign",
            sequence=1,
            batch_id="foreign-batch",
            required=True,
            outcome=MarketBookAttemptOutcome.CRASH_PENDING,
        )

    first = issue(
        plan,
        attempt_id="same",
        sequence=1,
        batch_index=0,
        required=True,
        outcome=MarketBookAttemptOutcome.CRASH_PENDING,
    )
    second = issue(
        plan,
        attempt_id="same",
        sequence=2,
        batch_index=0,
        required=True,
        outcome=MarketBookAttemptOutcome.TRANSPORT_FAILURE,
        previous=first,
    )
    with pytest.raises(MarketBookAttemptHistoryError, match="attempt_id cannot be reused"):
        MarketBookAttemptHistory(plan, (first, second))


def test_sequence_and_hash_chain_are_strictly_contiguous():
    plan = MarketBookReadPlan(("1.1",), "OPEN")
    first = issue(
        plan,
        attempt_id="a1",
        sequence=1,
        batch_index=0,
        required=True,
        outcome=MarketBookAttemptOutcome.CRASH_PENDING,
    )
    with pytest.raises(MarketBookAttemptHistoryError, match="contiguous"):
        issue(
            plan,
            attempt_id="a3",
            sequence=3,
            batch_index=0,
            required=True,
            outcome=MarketBookAttemptOutcome.TRANSPORT_FAILURE,
            previous=first,
        )

    second = issue(
        plan,
        attempt_id="a2",
        sequence=2,
        batch_index=0,
        required=True,
        outcome=MarketBookAttemptOutcome.TRANSPORT_FAILURE,
        previous=first,
    )
    object.__setattr__(second, "previous_record_id", "0" * 64)
    with pytest.raises(MarketBookAttemptHistoryError, match="predecessor"):
        MarketBookAttemptHistory(plan, (first, second))


def test_record_and_receipt_tampering_fail_canonical_recomputation():
    plan = MarketBookReadPlan(("1.1",), "OPEN")
    receipt = exact_receipt(plan, 0)
    record = issue(
        plan,
        attempt_id="exact",
        sequence=1,
        batch_index=0,
        required=True,
        outcome=MarketBookAttemptOutcome.EXACT_RESPONSE,
        receipt=receipt,
    )
    object.__setattr__(record, "required", False)
    with pytest.raises(MarketBookAttemptHistoryError, match="record_id"):
        MarketBookAttemptHistory(plan, (record,))

    clean = issue(
        plan,
        attempt_id="clean",
        sequence=1,
        batch_index=0,
        required=True,
        outcome=MarketBookAttemptOutcome.EXACT_RESPONSE,
        receipt=exact_receipt(plan, 0),
    )
    object.__setattr__(clean.exact_receipt, "receipt_id", "0" * 64)
    with pytest.raises(MarketBookAttemptHistoryError, match="canonical validation"):
        MarketBookAttemptHistory(plan, (clean,))


def test_restart_round_trip_is_deterministic_and_tamper_evident():
    plan = two_batch_plan()
    first = issue(
        plan,
        attempt_id="gap",
        sequence=1,
        batch_index=0,
        required=True,
        outcome=MarketBookAttemptOutcome.NOT_DISPATCHED_CONCURRENCY,
    )
    second = issue(
        plan,
        attempt_id="retry",
        sequence=2,
        batch_index=0,
        required=True,
        outcome=MarketBookAttemptOutcome.EXACT_RESPONSE,
        receipt=exact_receipt(plan, 0),
        previous=first,
    )
    third = issue(
        plan,
        attempt_id="other",
        sequence=3,
        batch_index=1,
        required=True,
        outcome=MarketBookAttemptOutcome.EXACT_RESPONSE,
        receipt=exact_receipt(plan, 1),
        previous=second,
    )
    history = MarketBookAttemptHistory(plan, (first, second, third))
    restored = MarketBookAttemptHistory.from_json(plan, history.to_json())
    assert restored == history
    assert restored.history_id == history.history_id
    assert restored.to_json() == history.to_json()
    assert restored.current_structural_complete is True
    assert restored.historical_required_gap_free is False

    raw = json.loads(history.to_json())
    raw["evidence"]["records"][0]["outcome"] = "EXACT_RESPONSE"
    with pytest.raises(MarketBookAttemptHistoryError):
        MarketBookAttemptHistory.from_json(plan, json.dumps(raw))


def test_history_is_bound_to_exact_plan_identity_on_restart():
    plan = MarketBookReadPlan(("1.1",), "OPEN")
    record = issue(
        plan,
        attempt_id="a1",
        sequence=1,
        batch_index=0,
        required=True,
        outcome=MarketBookAttemptOutcome.EXACT_RESPONSE,
        receipt=exact_receipt(plan, 0),
    )
    history = MarketBookAttemptHistory(plan, (record,))
    other_plan = MarketBookReadPlan(("1.2",), "OPEN")
    with pytest.raises(MarketBookAttemptHistoryError):
        MarketBookAttemptHistory.from_json(other_plan, history.to_json())


def test_no_positive_remote_dispatch_or_execution_authority_is_minted():
    plan = MarketBookReadPlan(("1.1",), "OPEN")
    record = issue(
        plan,
        attempt_id="a1",
        sequence=1,
        batch_index=0,
        required=True,
        outcome=MarketBookAttemptOutcome.EXACT_RESPONSE,
        receipt=exact_receipt(plan, 0),
    )
    evidence = MarketBookAttemptHistory(plan, (record,)).evidence_payload
    assert evidence["current_structural_complete"] is True
    assert evidence["historical_required_gap_free"] is True
    assert evidence["campaign_denominator_complete"] is False
    assert evidence["provider_observation_authenticated"] is False
    assert evidence["provider_freshness_proven"] is False
    assert evidence["provider_dispatch_authorized"] is False
    assert evidence["execution_authorized"] is False
