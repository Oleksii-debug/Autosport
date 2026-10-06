from datetime import datetime, timedelta, timezone
from pathlib import Path
import json

import pytest

import autosport.betfair_marketbook_retry_backoff as _retry_backoff_module
from autosport.betfair_marketbook_attempt_history import MarketBookAttemptOutcome
from autosport.betfair_marketbook_batch_plan import MarketBookReadPlan
from autosport.betfair_marketbook_retry_backoff import (
    MARKETBOOK_INCOMPLETE_RETRY_BASE_DELAY_US,
    MARKETBOOK_INCOMPLETE_RETRY_MAX_DELAY_US,
    MARKETBOOK_MAX_INCOMPLETE_AUTOMATIC_RETRIES,
    MARKETBOOK_RETRY_BACKOFF_POLICY_VERSION,
    MarketBookRetryBackoffError,
    MarketBookRetryBackoffGate,
    MarketBookRetryBackoffState,
    MarketBookRetryDisposition,
)
from autosport.continuous_observation import ContinuousObservationConfig
from autosport.ingestion_health import SourceHealthState


NOW = datetime(2026, 10, 6, 21, 0, tzinfo=timezone.utc)
SOURCE_ID = "betfair-marketbook"


def _plan(market_count: int = 1) -> MarketBookReadPlan:
    return MarketBookReadPlan(
        market_ids=tuple(f"1.{index:03d}" for index in range(1, market_count + 1)),
        market_status="OPEN",
        price_data=("EX_ALL_OFFERS",),
    )


def _gate(plan: MarketBookReadPlan, *, state=None) -> MarketBookRetryBackoffGate:
    return MarketBookRetryBackoffGate(
        plan,
        provider_source_id=SOURCE_ID,
        state=state,
    )


def _config(
    *,
    interval_seconds: float = 1.0,
    max_backoff_seconds: float = 8.0,
) -> ContinuousObservationConfig:
    return ContinuousObservationConfig(
        workspace=Path("."),
        interval_seconds=interval_seconds,
        max_backoff_seconds=max_backoff_seconds,
    )


def _health(
    streak: int,
    *,
    source_id: str = SOURCE_ID,
    last_error_at: datetime = NOW,
) -> SourceHealthState:
    return SourceHealthState(
        source_id=source_id,
        status="failed",
        poll_count=streak,
        total_failures=streak,
        consecutive_failures=streak,
        last_error_at=last_error_at.isoformat(),
        last_error="provider unavailable",
        last_failure_kind="provider_unavailable",
        consecutive_failure_kind_count=streak,
    )


def _at(microseconds: int) -> datetime:
    return NOW + timedelta(microseconds=microseconds)


def test_new_batch_is_ready_without_provider_or_execution_authority():
    plan = _plan()
    batch = plan.batches[0]
    gate = _gate(plan)

    decision = gate.admit(batch.batch_id, observed_at=NOW)

    assert decision.allowed is True
    assert decision.disposition is MarketBookRetryDisposition.READY
    assert decision.provider_recovery_projection_applied is False
    assert decision.provider_limit_coverage_complete is False
    assert decision.provider_dispatch_authorized is False
    assert decision.execution_authorized is False


@pytest.mark.parametrize(
    "provider_code",
    ("TOO_MANY_REQUESTS", "SERVICE_BUSY", "TIMEOUT_ERROR"),
)
def test_transient_provider_error_requires_canonical_provider_recovery(provider_code):
    plan = _plan()
    batch = plan.batches[0]
    gate = _gate(plan)

    state = gate.record_outcome(
        batch.batch_id,
        observed_at=NOW,
        outcome=MarketBookAttemptOutcome.PROVIDER_FAILURE,
        provider_error_code=provider_code,
    )
    entry = state.batches[0]

    assert entry.provider_recovery_required is True
    assert entry.next_eligible_at_utc_us is None
    assert entry.consecutive_incomplete_failures == 0
    assert entry.last_provider_error_code == provider_code

    decision = gate.admit(batch.batch_id, observed_at=NOW)
    assert decision.allowed is False
    assert (
        decision.disposition
        is MarketBookRetryDisposition.PROVIDER_RECOVERY_REQUIRED
    )
    assert decision.next_eligible_at_utc_us is None


def test_provider_recovery_deadline_reuses_canonical_health_streak_policy():
    plan = _plan()
    batch = plan.batches[0]
    gate = _gate(plan)
    gate.record_outcome(
        batch.batch_id,
        observed_at=NOW,
        outcome=MarketBookAttemptOutcome.PROVIDER_FAILURE,
        provider_error_code="SERVICE_BUSY",
    )

    config = _config(interval_seconds=1.0, max_backoff_seconds=8.0)
    health = _health(2)
    projected = gate.apply_provider_recovery(
        batch.batch_id,
        observed_at=NOW,
        health=health,
        config=config,
    )

    assert projected.allowed is False
    assert projected.disposition is MarketBookRetryDisposition.BACKOFF
    assert projected.provider_recovery_projection_applied is True
    assert projected.consecutive_retryable_failures == 2
    assert projected.next_eligible_at_utc_us is not None

    before = gate.admit(
        batch.batch_id,
        observed_at=NOW + timedelta(seconds=2) - timedelta(microseconds=1),
    )
    assert before.disposition is MarketBookRetryDisposition.BACKOFF

    ready = gate.admit(
        batch.batch_id,
        observed_at=NOW + timedelta(seconds=2),
    )
    assert ready.allowed is True
    assert ready.disposition is MarketBookRetryDisposition.READY
    assert ready.provider_recovery_projection_applied is True


def test_restart_requires_provider_health_revalidation_again():
    plan = _plan()
    batch = plan.batches[0]
    gate = _gate(plan)
    gate.record_outcome(
        batch.batch_id,
        observed_at=NOW,
        outcome=MarketBookAttemptOutcome.PROVIDER_FAILURE,
        provider_error_code="TIMEOUT_ERROR",
    )
    gate.apply_provider_recovery(
        batch.batch_id,
        observed_at=NOW,
        health=_health(1),
        config=_config(),
    )

    persisted = gate.snapshot()
    restored = _gate(plan, state=persisted)
    decision = restored.admit(batch.batch_id, observed_at=NOW)

    assert (
        decision.disposition
        is MarketBookRetryDisposition.PROVIDER_RECOVERY_REQUIRED
    )
    assert decision.provider_recovery_projection_applied is False


def test_provider_recovery_rejects_health_from_another_source():
    plan = _plan()
    batch = plan.batches[0]
    gate = _gate(plan)
    gate.record_outcome(
        batch.batch_id,
        observed_at=NOW,
        outcome=MarketBookAttemptOutcome.PROVIDER_FAILURE,
        provider_error_code="SERVICE_BUSY",
    )

    with pytest.raises(
        MarketBookRetryBackoffError,
        match="another source",
    ):
        gate.apply_provider_recovery(
            batch.batch_id,
            observed_at=NOW,
            health=_health(1, source_id="other-provider"),
            config=_config(),
        )


def test_provider_recovery_requires_typed_durable_provider_unavailable_health():
    plan = _plan()
    batch = plan.batches[0]
    gate = _gate(plan)
    gate.record_outcome(
        batch.batch_id,
        observed_at=NOW,
        outcome=MarketBookAttemptOutcome.PROVIDER_FAILURE,
        provider_error_code="TOO_MANY_REQUESTS",
    )
    health = SourceHealthState(
        source_id=SOURCE_ID,
        status="failed",
        poll_count=1,
        total_failures=1,
        consecutive_failures=1,
        last_error_at=NOW.isoformat(),
        last_error="generic failure",
        last_failure_kind="provider_or_validation",
        consecutive_failure_kind_count=1,
    )

    with pytest.raises(
        MarketBookRetryBackoffError,
        match="durable provider_unavailable health",
    ):
        gate.apply_provider_recovery(
            batch.batch_id,
            observed_at=NOW,
            health=health,
            config=_config(),
        )


@pytest.mark.parametrize(
    "provider_code",
    ("TOO_MUCH_DATA", "REQUEST_SIZE_EXCEEDS_LIMIT", "INVALID_INPUT_DATA", None),
)
def test_contract_or_unknown_provider_errors_are_terminal(provider_code):
    plan = _plan()
    batch = plan.batches[0]
    gate = _gate(plan)

    state = gate.record_outcome(
        batch.batch_id,
        observed_at=NOW,
        outcome=MarketBookAttemptOutcome.PROVIDER_FAILURE,
        provider_error_code=provider_code,
    )

    assert state.batches[0].terminal_failure is True
    decision = gate.admit(batch.batch_id, observed_at=NOW)
    assert decision.disposition is MarketBookRetryDisposition.TERMINAL
    assert decision.allowed is False


def test_incomplete_response_has_narrow_batch_local_bounded_retry():
    plan = _plan()
    batch = plan.batches[0]
    gate = _gate(plan)

    state = gate.record_outcome(
        batch.batch_id,
        observed_at=NOW,
        outcome=MarketBookAttemptOutcome.INCOMPLETE_RESPONSE,
    )
    entry = state.batches[0]

    assert entry.provider_recovery_required is False
    assert entry.consecutive_incomplete_failures == 1
    assert entry.next_eligible_at_utc_us == (
        state.last_observed_at_utc_us
        + MARKETBOOK_INCOMPLETE_RETRY_BASE_DELAY_US
    )
    assert gate.admit(
        batch.batch_id,
        observed_at=_at(MARKETBOOK_INCOMPLETE_RETRY_BASE_DELAY_US - 1),
    ).disposition is MarketBookRetryDisposition.BACKOFF


def test_incomplete_retry_exponential_delay_caps_and_exhausts():
    plan = _plan()
    batch = plan.batches[0]
    gate = _gate(plan)
    observed = 0
    delays = []

    for _ in range(MARKETBOOK_MAX_INCOMPLETE_AUTOMATIC_RETRIES):
        state = gate.record_outcome(
            batch.batch_id,
            observed_at=_at(observed),
            outcome=MarketBookAttemptOutcome.INCOMPLETE_RESPONSE,
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
        MARKETBOOK_INCOMPLETE_RETRY_MAX_DELAY_US,
    ]

    state = gate.record_outcome(
        batch.batch_id,
        observed_at=_at(observed),
        outcome=MarketBookAttemptOutcome.INCOMPLETE_RESPONSE,
    )
    entry = state.batches[0]
    assert entry.automatic_retry_exhausted is True
    assert entry.next_eligible_at_utc_us is None
    assert gate.admit(
        batch.batch_id,
        observed_at=_at(observed),
    ).disposition is MarketBookRetryDisposition.EXHAUSTED


def test_success_clears_batch_projection_without_erasing_external_attempt_history():
    plan = _plan()
    batch = plan.batches[0]
    gate = _gate(plan)
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
    assert gate.admit(batch.batch_id, observed_at=NOW).allowed is True


@pytest.mark.parametrize(
    "outcome",
    (
        MarketBookAttemptOutcome.NOT_DISPATCHED_RATE,
        MarketBookAttemptOutcome.NOT_DISPATCHED_CONCURRENCY,
        MarketBookAttemptOutcome.NOT_DISPATCHED_BACKOFF,
    ),
)
def test_local_non_dispatch_does_not_consume_or_rewrite_retry_state(outcome):
    plan = _plan()
    batch = plan.batches[0]
    gate = _gate(plan)
    first = gate.record_outcome(
        batch.batch_id,
        observed_at=NOW,
        outcome=MarketBookAttemptOutcome.INCOMPLETE_RESPONSE,
    )

    after = gate.record_outcome(
        batch.batch_id,
        observed_at=NOW,
        outcome=outcome,
    )

    assert after.batches == first.batches


def test_one_blocked_batch_does_not_starve_unrelated_batch():
    plan = _plan(12)
    assert len(plan.batches) == 2
    first, second = plan.batches
    gate = _gate(plan)

    gate.record_outcome(
        first.batch_id,
        observed_at=NOW,
        outcome=MarketBookAttemptOutcome.PROVIDER_FAILURE,
        provider_error_code="TOO_MANY_REQUESTS",
    )

    assert (
        gate.admit(first.batch_id, observed_at=NOW).disposition
        is MarketBookRetryDisposition.PROVIDER_RECOVERY_REQUIRED
    )
    assert gate.admit(second.batch_id, observed_at=NOW).allowed is True


def test_restart_round_trip_preserves_fail_closed_projection_not_runtime_deadline():
    plan = _plan()
    batch = plan.batches[0]
    gate = _gate(plan)
    state = gate.record_outcome(
        batch.batch_id,
        observed_at=NOW,
        outcome=MarketBookAttemptOutcome.PROVIDER_FAILURE,
        provider_error_code="SERVICE_BUSY",
    )

    encoded = state.to_json()
    restored_state = MarketBookRetryBackoffState.from_json(plan, encoded)
    restored = _gate(plan, state=restored_state)

    assert restored.snapshot() == state
    assert "provider_recovery_deadline_is_authoritative":false" in encoded
    assert "timer" not in encoded.lower()
    assert restored.admit(
        batch.batch_id,
        observed_at=NOW,
    ).disposition is MarketBookRetryDisposition.PROVIDER_RECOVERY_REQUIRED


def test_restart_rejects_state_bound_to_another_plan_or_source():
    plan = _plan()
    other_plan = MarketBookReadPlan(
        market_ids=("9.999",),
        market_status="OPEN",
        price_data=("EX_ALL_OFFERS",),
    )
    state = _gate(plan).snapshot()

    with pytest.raises(MarketBookRetryBackoffError, match="another MarketBook plan"):
        MarketBookRetryBackoffState.from_json(other_plan, state.to_json())

    with pytest.raises(
        MarketBookRetryBackoffError,
        match="another MarketBook plan/source",
    ):
        MarketBookRetryBackoffGate(
            plan,
            provider_source_id="other-provider",
            state=state,
        )


def test_snapshot_mutation_cannot_rewrite_live_retry_state():
    plan = _plan()
    batch = plan.batches[0]
    gate = _gate(plan)
    snapshot = gate.record_outcome(
        batch.batch_id,
        observed_at=NOW,
        outcome=MarketBookAttemptOutcome.INCOMPLETE_RESPONSE,
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
    source_gate = _gate(plan)
    state = source_gate.record_outcome(
        batch.batch_id,
        observed_at=NOW,
        outcome=MarketBookAttemptOutcome.INCOMPLETE_RESPONSE,
    )
    imported = state.batches[0]
    restored = _gate(plan, state=state)

    object.__setattr__(imported, "automatic_retry_exhausted", True)
    object.__setattr__(imported, "next_eligible_at_utc_us", None)

    live = restored.snapshot().batches[0]
    assert live is not imported
    assert live.automatic_retry_exhausted is False
    assert live.next_eligible_at_utc_us is not None


def test_backwards_time_and_datetime_subclass_fail_before_state_rewrite():
    class HostileDateTime(datetime):
        pass

    plan = _plan()
    batch = plan.batches[0]
    gate = _gate(plan)
    gate.admit(batch.batch_id, observed_at=_at(10))
    before = gate.snapshot()

    with pytest.raises(MarketBookRetryBackoffError, match="must not move backwards"):
        gate.admit(batch.batch_id, observed_at=NOW)
    assert gate.snapshot() == before

    hostile = HostileDateTime(2026, 10, 6, 21, 0, tzinfo=timezone.utc)
    with pytest.raises(MarketBookRetryBackoffError, match="exact datetime"):
        gate.admit(batch.batch_id, observed_at=hostile)
    assert gate.snapshot() == before


def test_serialized_projection_cannot_claim_provider_or_execution_authority():
    plan = _plan()
    envelope = json.loads(_gate(plan).snapshot().to_json())
    evidence = envelope["evidence"]
    assert evidence["provider_recovery_deadline_is_authoritative"] is False
    assert evidence["provider_limit_coverage_complete"] is False
    assert evidence["provider_dispatch_authorized"] is False
    assert evidence["provider_observation_authenticated"] is False
    assert evidence["provider_freshness_proven"] is False
    assert evidence["execution_authorized"] is False


def test_policy_version_declares_projection_not_second_provider_authority():
    assert MARKETBOOK_RETRY_BACKOFF_POLICY_VERSION == (
        "betfair.list-market-book.retry-backoff-projection.v2"
    )


def test_provider_recovery_rejects_health_predating_current_marketbook_failure():
    plan = _plan()
    batch = plan.batches[0]
    gate = _gate(plan)
    failure_at = NOW + timedelta(seconds=10)
    gate.record_outcome(
        batch.batch_id,
        observed_at=failure_at,
        outcome=MarketBookAttemptOutcome.PROVIDER_FAILURE,
        provider_error_code="SERVICE_BUSY",
    )

    with pytest.raises(
        MarketBookRetryBackoffError,
        match="predates current MarketBook failure",
    ):
        gate.apply_provider_recovery(
            batch.batch_id,
            observed_at=failure_at,
            health=_health(1, last_error_at=NOW),
            config=_config(),
        )


def test_provider_recovery_seals_canonical_health_validator_and_backoff_helper(
    monkeypatch,
):
    plan = _plan()
    batch = plan.batches[0]
    gate = _gate(plan)
    gate.record_outcome(
        batch.batch_id,
        observed_at=NOW,
        outcome=MarketBookAttemptOutcome.PROVIDER_FAILURE,
        provider_error_code="TIMEOUT_ERROR",
    )
    health = _health(1)
    config = _config(interval_seconds=1.0, max_backoff_seconds=8.0)

    monkeypatch.setattr(
        SourceHealthState,
        "validate",
        lambda self: (_ for _ in ()).throw(
            AssertionError("rebound health validator must not run")
        ),
    )
    monkeypatch.setattr(
        _retry_backoff_module,
        "_provider_backoff_seconds",
        lambda *args, **kwargs: 999999.0,
    )

    decision = gate.apply_provider_recovery(
        batch.batch_id,
        observed_at=NOW,
        health=health,
        config=config,
    )

    assert decision.disposition is MarketBookRetryDisposition.BACKOFF
    assert (
        decision.next_eligible_at_utc_us - decision.observed_at_utc_us
        == 1_000_000
    )
