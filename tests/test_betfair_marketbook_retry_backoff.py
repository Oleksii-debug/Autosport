from datetime import datetime, timedelta, timezone
import json

import pytest

from autosport.betfair_marketbook_attempt_history import MarketBookAttemptOutcome
from autosport.betfair_marketbook_batch_plan import MarketBookReadPlan
from autosport.betfair_marketbook_retry_backoff import (
    MARKETBOOK_MAX_AUTOMATIC_RETRIES,
    MARKETBOOK_RETRY_BASE_DELAY_US,
    MARKETBOOK_RETRY_MAX_DELAY_US,
    MARKETBOOK_RETRY_BACKOFF_POLICY_VERSION,
    MarketBookRetryBackoffError,
    MarketBookRetryBackoffGate,
    MarketBookRetryBackoffState,
    MarketBookRetryDisposition,
)


NOW = datetime(2026, 10, 6, 21, 0, tzinfo=timezone.utc)


def _plan(market_count: int = 1) -> MarketBookReadPlan:
    return MarketBookReadPlan(
        market_ids=tuple(f"1.{index:03d}" for index in range(1, market_count + 1)),
        market_status="OPEN",
        price_data=("EX_ALL_OFFERS",),
    )


def _at(microseconds: int) -> datetime:
    return NOW + timedelta(microseconds=microseconds)


def test_new_batch_is_ready_without_granting_provider_authority():
    plan = _plan()
    batch = plan.batches[0]
    gate = MarketBookRetryBackoffGate(plan)

    decision = gate.admit(batch.batch_id, observed_at=NOW)

    assert decision.allowed is True
    assert decision.disposition is MarketBookRetryDisposition.READY
    assert decision.consecutive_retryable_failures == 0
    assert decision.next_eligible_at_utc_us is None
    assert decision.provider_limit_coverage_complete is False
    assert decision.provider_dispatch_authorized is False
    assert decision.execution_authorized is False


@pytest.mark.parametrize(
    "provider_code",
    ("TOO_MANY_REQUESTS", "SERVICE_BUSY", "TIMEOUT_ERROR"),
)
def test_documented_transient_provider_errors_enter_bounded_backoff(provider_code):
    plan = _plan()
    batch = plan.batches[0]
    gate = MarketBookRetryBackoffGate(plan)

    state = gate.record_outcome(
        batch.batch_id,
        observed_at=NOW,
        outcome=MarketBookAttemptOutcome.PROVIDER_FAILURE,
        provider_error_code=provider_code,
    )
    entry = state.batches[0]

    assert entry.consecutive_retryable_failures == 1
    assert entry.next_eligible_at_utc_us == (
        state.last_observed_at_utc_us + MARKETBOOK_RETRY_BASE_DELAY_US
    )
    assert entry.automatic_retry_exhausted is False
    assert entry.terminal_failure is False
    assert entry.last_provider_error_code == provider_code

    blocked = gate.admit(
        batch.batch_id,
        observed_at=_at(MARKETBOOK_RETRY_BASE_DELAY_US - 1),
    )
    assert blocked.allowed is False
    assert blocked.disposition is MarketBookRetryDisposition.BACKOFF

    ready = gate.admit(
        batch.batch_id,
        observed_at=_at(MARKETBOOK_RETRY_BASE_DELAY_US),
    )
    assert ready.allowed is True
    assert ready.disposition is MarketBookRetryDisposition.READY


def test_backoff_exponentially_increases_and_caps():
    plan = _plan()
    batch = plan.batches[0]
    gate = MarketBookRetryBackoffGate(plan)
    observed = 0
    delays = []

    for failure_number in range(1, MARKETBOOK_MAX_AUTOMATIC_RETRIES + 1):
        state = gate.record_outcome(
            batch.batch_id,
            observed_at=_at(observed),
            outcome=MarketBookAttemptOutcome.PROVIDER_FAILURE,
            provider_error_code="SERVICE_BUSY",
        )
        entry = state.batches[0]
        delay = entry.next_eligible_at_utc_us - state.last_observed_at_utc_us
        delays.append(delay)
        observed += delay

    assert delays == [
        250_000,
        500_000,
        1_000_000,
        2_000_000,
        MARKETBOOK_RETRY_MAX_DELAY_US,
    ]
    assert max(delays) == MARKETBOOK_RETRY_MAX_DELAY_US


def test_retry_budget_exhausts_after_maximum_automatic_retries():
    plan = _plan()
    batch = plan.batches[0]
    gate = MarketBookRetryBackoffGate(plan)
    observed = 0

    for _ in range(MARKETBOOK_MAX_AUTOMATIC_RETRIES + 1):
        state = gate.record_outcome(
            batch.batch_id,
            observed_at=_at(observed),
            outcome=MarketBookAttemptOutcome.PROVIDER_FAILURE,
            provider_error_code="TOO_MANY_REQUESTS",
        )
        entry = state.batches[0]
        if entry.next_eligible_at_utc_us is not None:
            observed = entry.next_eligible_at_utc_us - state.last_observed_at_utc_us + observed

    entry = state.batches[0]
    assert entry.consecutive_retryable_failures == MARKETBOOK_MAX_AUTOMATIC_RETRIES + 1
    assert entry.automatic_retry_exhausted is True
    assert entry.next_eligible_at_utc_us is None

    decision = gate.admit(batch.batch_id, observed_at=_at(observed))
    assert decision.allowed is False
    assert decision.disposition is MarketBookRetryDisposition.EXHAUSTED


@pytest.mark.parametrize(
    "provider_code",
    ("TOO_MUCH_DATA", "REQUEST_SIZE_EXCEEDS_LIMIT", "INVALID_INPUT_DATA", None),
)
def test_contract_or_unknown_provider_errors_are_terminal_not_auto_retry(provider_code):
    plan = _plan()
    batch = plan.batches[0]
    gate = MarketBookRetryBackoffGate(plan)

    state = gate.record_outcome(
        batch.batch_id,
        observed_at=NOW,
        outcome=MarketBookAttemptOutcome.PROVIDER_FAILURE,
        provider_error_code=provider_code,
    )

    assert state.batches[0].terminal_failure is True
    assert state.batches[0].next_eligible_at_utc_us is None
    decision = gate.admit(batch.batch_id, observed_at=NOW)
    assert decision.allowed is False
    assert decision.disposition is MarketBookRetryDisposition.TERMINAL


def test_success_resets_prior_backoff_without_erasing_external_attempt_history():
    plan = _plan()
    batch = plan.batches[0]
    gate = MarketBookRetryBackoffGate(plan)

    gate.record_outcome(
        batch.batch_id,
        observed_at=NOW,
        outcome=MarketBookAttemptOutcome.PROVIDER_FAILURE,
        provider_error_code="SERVICE_BUSY",
    )
    state = gate.record_outcome(
        batch.batch_id,
        observed_at=NOW,
        outcome=MarketBookAttemptOutcome.EXACT_RESPONSE,
    )

    assert state.batches == ()
    decision = gate.admit(batch.batch_id, observed_at=NOW)
    assert decision.allowed is True


def test_incomplete_response_uses_same_bounded_retry_budget():
    plan = _plan()
    batch = plan.batches[0]
    gate = MarketBookRetryBackoffGate(plan)

    state = gate.record_outcome(
        batch.batch_id,
        observed_at=NOW,
        outcome=MarketBookAttemptOutcome.INCOMPLETE_RESPONSE,
    )

    entry = state.batches[0]
    assert entry.last_outcome is MarketBookAttemptOutcome.INCOMPLETE_RESPONSE
    assert entry.consecutive_retryable_failures == 1
    assert entry.terminal_failure is False


@pytest.mark.parametrize(
    "outcome",
    (
        MarketBookAttemptOutcome.NOT_DISPATCHED_RATE,
        MarketBookAttemptOutcome.NOT_DISPATCHED_CONCURRENCY,
        MarketBookAttemptOutcome.NOT_DISPATCHED_BACKOFF,
    ),
)
def test_local_non_dispatch_does_not_consume_retry_budget(outcome):
    plan = _plan()
    batch = plan.batches[0]
    gate = MarketBookRetryBackoffGate(plan)

    first = gate.record_outcome(
        batch.batch_id,
        observed_at=NOW,
        outcome=MarketBookAttemptOutcome.PROVIDER_FAILURE,
        provider_error_code="SERVICE_BUSY",
    )
    after = gate.record_outcome(
        batch.batch_id,
        observed_at=NOW,
        outcome=outcome,
    )

    assert after.batches == first.batches


def test_one_backed_off_batch_does_not_block_unrelated_batch():
    plan = _plan(12)
    assert len(plan.batches) == 2
    first, second = plan.batches
    gate = MarketBookRetryBackoffGate(plan)

    gate.record_outcome(
        first.batch_id,
        observed_at=NOW,
        outcome=MarketBookAttemptOutcome.PROVIDER_FAILURE,
        provider_error_code="TOO_MANY_REQUESTS",
    )

    first_decision = gate.admit(first.batch_id, observed_at=NOW)
    second_decision = gate.admit(second.batch_id, observed_at=NOW)

    assert first_decision.disposition is MarketBookRetryDisposition.BACKOFF
    assert second_decision.allowed is True
    assert second_decision.disposition is MarketBookRetryDisposition.READY


def test_restart_round_trip_preserves_exact_backoff_and_no_timer_identity():
    plan = _plan(12)
    first = plan.batches[0]
    gate = MarketBookRetryBackoffGate(plan)
    state = gate.record_outcome(
        first.batch_id,
        observed_at=NOW,
        outcome=MarketBookAttemptOutcome.PROVIDER_FAILURE,
        provider_error_code="TIMEOUT_ERROR",
    )

    encoded = state.to_json()
    restored_state = MarketBookRetryBackoffState.from_json(plan, encoded)
    restored_gate = MarketBookRetryBackoffGate(plan, state=restored_state)

    assert restored_gate.snapshot() == state
    assert restored_state.state_id == state.state_id
    assert "timer" not in encoded.lower()
    assert "session" not in encoded.lower()
    assert "credential" not in encoded.lower()


def test_restart_rejects_state_bound_to_another_plan():
    plan = _plan()
    other_plan = MarketBookReadPlan(
        market_ids=("9.999",),
        market_status="OPEN",
        price_data=("EX_ALL_OFFERS",),
    )
    gate = MarketBookRetryBackoffGate(plan)
    encoded = gate.snapshot().to_json()

    with pytest.raises(MarketBookRetryBackoffError, match="another MarketBook plan"):
        MarketBookRetryBackoffState.from_json(other_plan, encoded)


def test_backwards_time_is_rejected_without_rewriting_state():
    plan = _plan()
    batch = plan.batches[0]
    gate = MarketBookRetryBackoffGate(plan)
    gate.admit(batch.batch_id, observed_at=_at(10))
    before = gate.snapshot()

    with pytest.raises(MarketBookRetryBackoffError, match="must not move backwards"):
        gate.admit(batch.batch_id, observed_at=NOW)

    assert gate.snapshot() == before


def test_datetime_subclass_is_rejected_before_state_mutation():
    class HostileDateTime(datetime):
        pass

    plan = _plan()
    batch = plan.batches[0]
    gate = MarketBookRetryBackoffGate(plan)
    before = gate.snapshot()
    hostile = HostileDateTime(
        2026, 10, 6, 21, 0, tzinfo=timezone.utc
    )

    with pytest.raises(MarketBookRetryBackoffError, match="exact datetime"):
        gate.admit(batch.batch_id, observed_at=hostile)

    assert gate.snapshot() == before


def test_serialized_state_cannot_claim_dispatch_or_execution_authority():
    plan = _plan()
    state = MarketBookRetryBackoffGate(plan).snapshot()
    envelope = json.loads(state.to_json())
    evidence = envelope["evidence"]
    assert evidence["provider_limit_coverage_complete"] is False
    assert evidence["provider_dispatch_authorized"] is False
    assert evidence["provider_observation_authenticated"] is False
    assert evidence["provider_freshness_proven"] is False
    assert evidence["execution_authorized"] is False


def test_policy_version_is_explicit_and_stable():
    assert MARKETBOOK_RETRY_BACKOFF_POLICY_VERSION == (
        "betfair.list-market-book.retry-backoff.v1"
    )


def test_snapshot_mutation_cannot_rewrite_live_backoff_state():
    plan = _plan()
    batch = plan.batches[0]
    gate = MarketBookRetryBackoffGate(plan)
    snapshot = gate.record_outcome(
        batch.batch_id,
        observed_at=NOW,
        outcome=MarketBookAttemptOutcome.PROVIDER_FAILURE,
        provider_error_code="SERVICE_BUSY",
    )
    exposed = snapshot.batches[0]
    object.__setattr__(exposed, "terminal_failure", True)
    object.__setattr__(exposed, "next_eligible_at_utc_us", None)

    live = gate.snapshot().batches[0]
    assert live is not exposed
    assert live.terminal_failure is False
    assert live.next_eligible_at_utc_us is not None


def test_constructor_detaches_caller_owned_restart_state():
    plan = _plan()
    batch = plan.batches[0]
    source_gate = MarketBookRetryBackoffGate(plan)
    state = source_gate.record_outcome(
        batch.batch_id,
        observed_at=NOW,
        outcome=MarketBookAttemptOutcome.PROVIDER_FAILURE,
        provider_error_code="TIMEOUT_ERROR",
    )
    imported = state.batches[0]
    restored_gate = MarketBookRetryBackoffGate(plan, state=state)

    object.__setattr__(imported, "automatic_retry_exhausted", True)
    object.__setattr__(imported, "next_eligible_at_utc_us", None)

    live = restored_gate.snapshot().batches[0]
    assert live is not imported
    assert live.automatic_retry_exhausted is False
    assert live.next_eligible_at_utc_us is not None
