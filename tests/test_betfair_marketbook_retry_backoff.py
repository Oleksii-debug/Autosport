from datetime import datetime, timedelta, timezone
from pathlib import Path
import json

import pytest

import autosport.betfair_marketbook_retry_backoff as _retry_module
from autosport.betfair_marketbook_attempt_history import MarketBookAttemptOutcome
from autosport.betfair_marketbook_batch_plan import MarketBookReadPlan
from autosport.betfair_marketbook_retry_backoff import (
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
        market_ids=tuple(
            f"1.{index:03d}" for index in range(1, market_count + 1)
        ),
        market_status="OPEN",
        price_data=("EX_ALL_OFFERS",),
    )


def _gate(
    plan: MarketBookReadPlan,
    *,
    state: MarketBookRetryBackoffState | None = None,
) -> MarketBookRetryBackoffGate:
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


def test_new_batch_is_ready_but_never_dispatch_authority():
    plan = _plan()
    batch = plan.batches[0]
    decision = _gate(plan).admit(batch.batch_id, observed_at=NOW)

    assert decision.allowed is True
    assert decision.disposition is MarketBookRetryDisposition.READY
    assert decision.provider_recovery_streak == 0
    assert decision.next_eligible_at_utc_us is None
    assert decision.provider_recovery_projection_applied is False
    assert decision.provider_limit_coverage_complete is False
    assert decision.provider_dispatch_authorized is False
    assert decision.execution_authorized is False


@pytest.mark.parametrize(
    "provider_code",
    ("TOO_MANY_REQUESTS", "SERVICE_BUSY", "TIMEOUT_ERROR"),
)
def test_transient_provider_error_requires_external_recovery_authority(
    provider_code,
):
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
    assert entry.provider_failure_observed_at_utc_us is not None
    assert entry.terminal_failure is False
    assert entry.last_provider_error_code == provider_code

    decision = gate.admit(batch.batch_id, observed_at=NOW)
    assert decision.allowed is False
    assert (
        decision.disposition
        is MarketBookRetryDisposition.PROVIDER_RECOVERY_REQUIRED
    )
    assert decision.next_eligible_at_utc_us is None
    assert decision.provider_recovery_projection_applied is False


def test_provider_recovery_reuses_exact_canonical_backoff_policy():
    plan = _plan()
    batch = plan.batches[0]
    gate = _gate(plan)
    gate.record_outcome(
        batch.batch_id,
        observed_at=NOW,
        outcome=MarketBookAttemptOutcome.PROVIDER_FAILURE,
        provider_error_code="SERVICE_BUSY",
    )

    projected = gate.apply_provider_recovery(
        batch.batch_id,
        observed_at=NOW,
        health=_health(2),
        config=_config(interval_seconds=1.0, max_backoff_seconds=8.0),
    )

    assert projected.allowed is False
    assert projected.disposition is MarketBookRetryDisposition.BACKOFF
    assert projected.provider_recovery_projection_applied is True
    assert projected.provider_recovery_streak == 2
    assert (
        projected.next_eligible_at_utc_us
        - projected.observed_at_utc_us
        == 2_000_000
    )

    before = gate.admit(
        batch.batch_id,
        observed_at=NOW + timedelta(seconds=2, microseconds=-1),
    )
    assert before.disposition is MarketBookRetryDisposition.BACKOFF

    ready = gate.admit(
        batch.batch_id,
        observed_at=NOW + timedelta(seconds=2),
    )
    assert ready.allowed is True
    assert ready.disposition is MarketBookRetryDisposition.READY
    assert ready.provider_recovery_projection_applied is True


def test_provider_backoff_caps_using_canonical_continuous_config():
    plan = _plan()
    batch = plan.batches[0]
    gate = _gate(plan)
    gate.record_outcome(
        batch.batch_id,
        observed_at=NOW,
        outcome=MarketBookAttemptOutcome.PROVIDER_FAILURE,
        provider_error_code="TOO_MANY_REQUESTS",
    )
    projected = gate.apply_provider_recovery(
        batch.batch_id,
        observed_at=NOW,
        health=_health(20),
        config=_config(interval_seconds=1.0, max_backoff_seconds=3.0),
    )
    assert (
        projected.next_eligible_at_utc_us
        - projected.observed_at_utc_us
        == 3_000_000
    )


def test_restart_discards_volatile_deadline_and_revalidates_durable_health():
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


def test_provider_recovery_rejects_wrong_source_and_wrong_failure_kind():
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

    generic = SourceHealthState(
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
            health=generic,
            config=_config(),
        )


def test_provider_recovery_rejects_health_predating_current_failure():
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


@pytest.mark.parametrize(
    ("outcome", "provider_code"),
    (
        (MarketBookAttemptOutcome.INCOMPLETE_RESPONSE, None),
        (MarketBookAttemptOutcome.TRANSPORT_FAILURE, None),
        (MarketBookAttemptOutcome.PARSE_FAILURE, None),
        (MarketBookAttemptOutcome.PROVIDER_FAILURE, "TOO_MUCH_DATA"),
        (
            MarketBookAttemptOutcome.PROVIDER_FAILURE,
            "REQUEST_SIZE_EXCEEDS_LIMIT",
        ),
        (MarketBookAttemptOutcome.PROVIDER_FAILURE, "INVALID_INPUT_DATA"),
        (MarketBookAttemptOutcome.PROVIDER_FAILURE, None),
    ),
)
def test_nontransient_failures_are_explicit_terminal_not_auto_retry(
    outcome,
    provider_code,
):
    plan = _plan()
    batch = plan.batches[0]
    gate = _gate(plan)

    state = gate.record_outcome(
        batch.batch_id,
        observed_at=NOW,
        outcome=outcome,
        provider_error_code=provider_code,
    )

    entry = state.batches[0]
    assert entry.terminal_failure is True
    assert entry.provider_recovery_required is False
    decision = gate.admit(batch.batch_id, observed_at=NOW)
    assert decision.allowed is False
    assert decision.disposition is MarketBookRetryDisposition.TERMINAL


@pytest.mark.parametrize(
    "outcome",
    (
        MarketBookAttemptOutcome.NOT_DISPATCHED_RATE,
        MarketBookAttemptOutcome.NOT_DISPATCHED_CONCURRENCY,
        MarketBookAttemptOutcome.NOT_DISPATCHED_BACKOFF,
    ),
)
def test_local_non_dispatch_does_not_rewrite_existing_state(outcome):
    plan = _plan()
    batch = plan.batches[0]
    gate = _gate(plan)
    before = gate.record_outcome(
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

    assert after.batches == before.batches


def test_success_clears_projection_but_external_history_remains_separate():
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


def test_one_provider_blocked_batch_does_not_starve_unrelated_batch():
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


def test_restart_round_trip_is_fail_closed_and_has_no_deadline_authority():
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
    envelope = json.loads(encoded)
    evidence = envelope["evidence"]
    assert evidence["provider_recovery_deadline_is_authoritative"] is False
    assert evidence["provider_recovery_streak_is_authoritative"] is False
    assert "timer" not in encoded.lower()
    assert (
        restored.admit(batch.batch_id, observed_at=NOW).disposition
        is MarketBookRetryDisposition.PROVIDER_RECOVERY_REQUIRED
    )


def test_restart_rejects_duplicate_json_keys_at_every_authority_level():
    plan = _plan()
    batch = plan.batches[0]
    state = _gate(plan).record_outcome(
        batch.batch_id,
        observed_at=NOW,
        outcome=MarketBookAttemptOutcome.PROVIDER_FAILURE,
        provider_error_code="SERVICE_BUSY",
    )
    canonical = json.loads(state.to_json())
    evidence = canonical["evidence"]
    batch_payload = evidence["batches"][0]

    duplicate_envelope = (
        '{"evidence":'
        + json.dumps(evidence, separators=(",", ":"))
        + ',"state_id":"'
        + canonical["state_id"]
        + '","state_id":"'
        + canonical["state_id"]
        + '"}'
    )
    with pytest.raises(
        MarketBookRetryBackoffError,
        match="duplicate JSON key: state_id",
    ):
        MarketBookRetryBackoffState.from_json(plan, duplicate_envelope)

    evidence_text = json.dumps(evidence, separators=(",", ":"))
    duplicate_batch = json.dumps(
        batch_payload,
        separators=(",", ":"),
    )[:-1] + ',"batch_id":"' + batch.batch_id + '"}'
    duplicate_nested = evidence_text.replace(
        json.dumps(batch_payload, separators=(",", ":")),
        duplicate_batch,
    )
    encoded = (
        '{"evidence":'
        + duplicate_nested
        + ',"state_id":"'
        + canonical["state_id"]
        + '"}'
    )
    with pytest.raises(
        MarketBookRetryBackoffError,
        match="duplicate JSON key: batch_id",
    ):
        MarketBookRetryBackoffState.from_json(plan, encoded)


@pytest.mark.parametrize("constant", ("NaN", "Infinity", "-Infinity"))
def test_restart_rejects_noncanonical_json_constants(constant):
    plan = _plan()
    encoded = (
        '{"evidence":'
        '{"schema":"betfair-marketbook-retry-backoff-projection-v3",'
        '"policy_version":"betfair.list-market-book.retry-backoff-projection.v3",'
        '"plan_id":' + json.dumps(plan.plan_id) + ','
        '"request_contract_id":'
        + json.dumps(plan.request_contract_id)
        + ',"provider_source_id":"betfair-marketbook",'
        '"last_observed_at_utc_us":'
        + constant
        + ',"batches":[],'
        '"provider_recovery_deadline_is_authoritative":false,'
        '"provider_recovery_streak_is_authoritative":false,'
        '"provider_limit_coverage_complete":false,'
        '"provider_dispatch_authorized":false,'
        '"provider_observation_authenticated":false,'
        '"provider_freshness_proven":false,'
        '"execution_authorized":false},'
        '"state_id":"' + "0" * 64 + '"}'
    )
    with pytest.raises(
        MarketBookRetryBackoffError,
        match="noncanonical JSON constant",
    ):
        MarketBookRetryBackoffState.from_json(plan, encoded)


def test_restart_rejects_state_bound_to_another_plan_or_source():
    plan = _plan()
    other_plan = MarketBookReadPlan(
        market_ids=("9.999",),
        market_status="OPEN",
        price_data=("EX_ALL_OFFERS",),
    )
    state = _gate(plan).snapshot()

    with pytest.raises(
        MarketBookRetryBackoffError,
        match="another MarketBook plan",
    ):
        MarketBookRetryBackoffState.from_json(
            other_plan,
            state.to_json(),
        )

    with pytest.raises(
        MarketBookRetryBackoffError,
        match="another MarketBook plan/source",
    ):
        MarketBookRetryBackoffGate(
            plan,
            provider_source_id="other-provider",
            state=state,
        )


def test_snapshot_and_constructor_detach_caller_owned_batch_state():
    plan = _plan()
    batch = plan.batches[0]
    gate = _gate(plan)
    snapshot = gate.record_outcome(
        batch.batch_id,
        observed_at=NOW,
        outcome=MarketBookAttemptOutcome.PROVIDER_FAILURE,
        provider_error_code="SERVICE_BUSY",
    )
    exposed = snapshot.batches[0]
    restored = _gate(plan, state=snapshot)

    object.__setattr__(exposed, "terminal_failure", True)
    object.__setattr__(exposed, "provider_recovery_required", False)

    live = gate.snapshot().batches[0]
    restored_live = restored.snapshot().batches[0]
    assert live is not exposed
    assert restored_live is not exposed
    assert live.provider_recovery_required is True
    assert restored_live.provider_recovery_required is True


def test_backwards_time_and_datetime_subclass_fail_before_state_rewrite():
    class HostileDateTime(datetime):
        pass

    plan = _plan()
    batch = plan.batches[0]
    gate = _gate(plan)
    gate.admit(batch.batch_id, observed_at=NOW + timedelta(microseconds=10))
    before = gate.snapshot()

    with pytest.raises(
        MarketBookRetryBackoffError,
        match="must not move backwards",
    ):
        gate.admit(batch.batch_id, observed_at=NOW)
    assert gate.snapshot() == before

    hostile = HostileDateTime(
        2026,
        10,
        6,
        21,
        0,
        tzinfo=timezone.utc,
    )
    with pytest.raises(
        MarketBookRetryBackoffError,
        match="exact datetime",
    ):
        gate.admit(batch.batch_id, observed_at=hostile)
    assert gate.snapshot() == before


def test_late_rebind_cannot_replace_canonical_provider_backoff_or_health_validator(
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
        _retry_module,
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
        decision.next_eligible_at_utc_us
        - decision.observed_at_utc_us
        == 1_000_000
    )


def test_serialized_projection_cannot_claim_provider_or_execution_authority():
    plan = _plan()
    evidence = json.loads(_gate(plan).snapshot().to_json())["evidence"]
    assert evidence["provider_recovery_deadline_is_authoritative"] is False
    assert evidence["provider_recovery_streak_is_authoritative"] is False
    assert evidence["provider_limit_coverage_complete"] is False
    assert evidence["provider_dispatch_authorized"] is False
    assert evidence["provider_observation_authenticated"] is False
    assert evidence["provider_freshness_proven"] is False
    assert evidence["execution_authorized"] is False


def test_policy_version_declares_projection_not_second_provider_authority():
    assert MARKETBOOK_RETRY_BACKOFF_POLICY_VERSION == (
        "betfair.list-market-book.retry-backoff-projection.v3"
    )


def test_gate_seals_late_module_enum_and_helper_rebinding(monkeypatch):
    plan = _plan()
    batch = plan.batches[0]
    gate = _gate(plan)

    monkeypatch.setattr(_retry_module, "MarketBookAttemptOutcome", object)
    monkeypatch.setattr(_retry_module, "MarketBookRetryDisposition", object)
    monkeypatch.setattr(
        _retry_module,
        "_provider_code",
        lambda value, optional=True: "FORGED",
    )
    monkeypatch.setattr(
        _retry_module,
        "_utc_microseconds",
        lambda value, name: -1,
    )

    state = gate.record_outcome(
        batch.batch_id,
        observed_at=NOW,
        outcome=MarketBookAttemptOutcome.PROVIDER_FAILURE,
        provider_error_code="SERVICE_BUSY",
    )
    assert state.batches[0].last_provider_error_code == "SERVICE_BUSY"

    decision = gate.admit(batch.batch_id, observed_at=NOW)
    assert (
        decision.disposition
        is MarketBookRetryDisposition.PROVIDER_RECOVERY_REQUIRED
    )
    assert decision.observed_at_utc_us > 0


def test_gate_revalidates_mutated_restart_state_after_lifecycle_rebind(
    monkeypatch,
):
    plan = _plan()
    batch = plan.batches[0]
    state = _gate(plan).record_outcome(
        batch.batch_id,
        observed_at=NOW,
        outcome=MarketBookAttemptOutcome.PROVIDER_FAILURE,
        provider_error_code="SERVICE_BUSY",
    )
    object.__setattr__(
        state.batches[0],
        "provider_failure_observed_at_utc_us",
        None,
    )

    monkeypatch.setattr(
        _retry_module.MarketBookRetryBatchState,
        "__post_init__",
        lambda self: None,
    )
    monkeypatch.setattr(
        _retry_module.MarketBookRetryBackoffState,
        "__post_init__",
        lambda self: None,
    )

    with pytest.raises(
        MarketBookRetryBackoffError,
        match="provider recovery state is contradictory",
    ):
        _gate(plan, state=state)


def test_restart_duplicate_rejection_is_sealed_against_helper_rebind(
    monkeypatch,
):
    plan = _plan()
    state = _gate(plan).snapshot()
    canonical = json.loads(state.to_json())
    evidence = json.dumps(
        canonical["evidence"],
        separators=(",", ":"),
    )
    encoded = (
        '{"evidence":'
        + evidence
        + ',"state_id":"'
        + canonical["state_id"]
        + '","state_id":"'
        + canonical["state_id"]
        + '"}'
    )

    monkeypatch.setattr(
        _retry_module,
        "_strict_json_object",
        lambda pairs: dict(pairs),
    )
    monkeypatch.setattr(
        _retry_module,
        "_reject_json_constant",
        lambda value: None,
    )

    with pytest.raises(
        MarketBookRetryBackoffError,
        match="duplicate JSON key: state_id",
    ):
        MarketBookRetryBackoffState.from_json(plan, encoded)
