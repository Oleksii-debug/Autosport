from __future__ import annotations

import math

import pytest

from autosport.matchbook_rate_governor import (
    MatchbookRateBucket,
    MatchbookRateDeferred,
    MatchbookRateGovernor,
    MatchbookRateGovernorError,
    MatchbookRatePolicy,
    MatchbookRatePriority,
    resolve_matchbook_rate_governor,
)


class FakeClock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def reports_policy(
    *,
    capacity: int = 40,
    reserve: int = 1,
    window: float = 60.0,
) -> dict[MatchbookRateBucket, MatchbookRatePolicy]:
    return {
        MatchbookRateBucket.REPORTS: MatchbookRatePolicy(
            capacity=capacity,
            window_seconds=window,
            safety_read_reserve=reserve,
        )
    }


def ready_reports(
    scope: str,
    *,
    capacity: int = 40,
    reserve: int = 1,
    window: float = 60.0,
):
    clock = FakeClock()
    governor = resolve_matchbook_rate_governor(
        scope,
        reports_policy(
            capacity=capacity,
            reserve=reserve,
            window=window,
        ),
        clock=clock,
    )
    clock.advance(max(window, 600.0))
    return governor, clock


def test_cold_start_quarantines_instead_of_minting_allowance() -> None:
    clock = FakeClock()
    governor = resolve_matchbook_rate_governor(
        "test:cold-start",
        reports_policy(window=60.0),
        clock=clock,
    )

    with pytest.raises(MatchbookRateDeferred) as exc_info:
        governor.admit(MatchbookRateBucket.REPORTS)
    assert exc_info.value.reason == "provider_rate_window_blocked"
    assert exc_info.value.retry_after_seconds == pytest.approx(600.0)

    clock.advance(600.0)
    receipt = governor.admit(MatchbookRateBucket.REPORTS)
    assert receipt.grants_execution_authority is False


def test_reports_budget_is_shared_across_components_and_reserves_safety_read() -> None:
    governor, clock = ready_reports("test:reports-shared")

    component_a = resolve_matchbook_rate_governor(
        "test:reports-shared",
        reports_policy(),
        clock=clock,
    )
    component_b = resolve_matchbook_rate_governor(
        "test:reports-shared",
        reports_policy(),
        clock=clock,
    )
    assert component_a is component_b is governor

    for _ in range(39):
        component_a.admit(MatchbookRateBucket.REPORTS)

    with pytest.raises(MatchbookRateDeferred) as exc_info:
        component_b.admit(MatchbookRateBucket.REPORTS)
    assert exc_info.value.reason == "safety_read_capacity_reserved"

    safety = component_b.admit(
        MatchbookRateBucket.REPORTS,
        priority=MatchbookRatePriority.SAFETY_READ,
    )
    assert safety.remaining_total == 0
    assert safety.grants_execution_authority is False

    with pytest.raises(MatchbookRateDeferred):
        component_a.admit(
            MatchbookRateBucket.REPORTS,
            priority=MatchbookRatePriority.SAFETY_READ,
        )


def test_events_and_reports_use_independent_provider_buckets() -> None:
    clock = FakeClock()
    policies = {
        MatchbookRateBucket.EVENTS: MatchbookRatePolicy(2, 60.0),
        MatchbookRateBucket.REPORTS: MatchbookRatePolicy(1, 60.0),
    }
    governor = resolve_matchbook_rate_governor(
        "test:bucket-isolation",
        policies,
        clock=clock,
    )
    clock.advance(600.0)

    governor.admit(MatchbookRateBucket.REPORTS)
    with pytest.raises(MatchbookRateDeferred):
        governor.admit(MatchbookRateBucket.REPORTS)

    governor.admit(MatchbookRateBucket.EVENTS)
    governor.admit(MatchbookRateBucket.EVENTS)
    with pytest.raises(MatchbookRateDeferred):
        governor.admit(MatchbookRateBucket.EVENTS)


def test_every_retry_consumes_another_request_token() -> None:
    governor, _ = ready_reports(
        "test:retry-cost",
        capacity=2,
        reserve=0,
    )

    first = governor.admit(MatchbookRateBucket.REPORTS)
    retry = governor.admit(MatchbookRateBucket.REPORTS)
    assert retry.sequence == first.sequence + 1

    with pytest.raises(MatchbookRateDeferred) as exc_info:
        governor.admit(MatchbookRateBucket.REPORTS)
    assert exc_info.value.reason == "provider_rate_capacity_exhausted"


def test_429_throttle_seen_by_one_reports_consumer_fences_siblings() -> None:
    governor, clock = ready_reports("test:shared-throttle")
    sibling = resolve_matchbook_rate_governor(
        "test:shared-throttle",
        reports_policy(),
        clock=clock,
    )

    governor.observe_throttle(
        MatchbookRateBucket.REPORTS,
        retry_after_seconds=30.0,
    )

    with pytest.raises(MatchbookRateDeferred) as exc_info:
        sibling.admit(MatchbookRateBucket.REPORTS)
    assert exc_info.value.retry_after_seconds == pytest.approx(600.0)

    clock.advance(599.0)
    with pytest.raises(MatchbookRateDeferred):
        sibling.admit(MatchbookRateBucket.REPORTS)

    clock.advance(1.0)
    sibling.admit(MatchbookRateBucket.REPORTS)


def test_provider_wide_throttle_is_explicit_and_bucket_local_is_not() -> None:
    clock = FakeClock()
    policies = {
        MatchbookRateBucket.EVENTS: MatchbookRatePolicy(2, 60.0),
        MatchbookRateBucket.REPORTS: MatchbookRatePolicy(2, 60.0),
    }
    governor = resolve_matchbook_rate_governor(
        "test:provider-wide",
        policies,
        clock=clock,
    )
    clock.advance(600.0)

    governor.observe_throttle(
        MatchbookRateBucket.REPORTS,
        retry_after_seconds=3.0,
        provider_wide=False,
    )
    governor.admit(MatchbookRateBucket.EVENTS)
    with pytest.raises(MatchbookRateDeferred):
        governor.admit(MatchbookRateBucket.REPORTS)

    clock.advance(600.0)
    governor.observe_throttle(
        MatchbookRateBucket.REPORTS,
        retry_after_seconds=4.0,
        provider_wide=True,
    )
    with pytest.raises(MatchbookRateDeferred):
        governor.admit(MatchbookRateBucket.EVENTS)


def test_reresolve_does_not_reset_active_block_or_budget() -> None:
    governor, clock = ready_reports(
        "test:reresolve",
        capacity=3,
        reserve=0,
    )
    governor.admit(MatchbookRateBucket.REPORTS)
    governor.observe_throttle(
        MatchbookRateBucket.REPORTS,
        retry_after_seconds=20.0,
    )

    same = resolve_matchbook_rate_governor(
        "test:reresolve",
        reports_policy(capacity=3, reserve=0),
        clock=clock,
    )
    assert same is governor

    with pytest.raises(MatchbookRateDeferred) as exc_info:
        same.admit(MatchbookRateBucket.REPORTS)
    assert exc_info.value.retry_after_seconds == pytest.approx(600.0)


def test_same_scope_policy_or_clock_rebinding_is_rejected() -> None:
    clock = FakeClock()
    governor = resolve_matchbook_rate_governor(
        "test:identity",
        reports_policy(),
        clock=clock,
    )
    assert (
        resolve_matchbook_rate_governor(
            "test:identity",
            reports_policy(),
            clock=clock,
        )
        is governor
    )

    with pytest.raises(
        MatchbookRateGovernorError,
        match="different rate policy",
    ):
        resolve_matchbook_rate_governor(
            "test:identity",
            reports_policy(capacity=39),
            clock=clock,
        )

    with pytest.raises(
        MatchbookRateGovernorError,
        match="different clock",
    ):
        resolve_matchbook_rate_governor(
            "test:identity",
            reports_policy(),
            clock=FakeClock(),
        )

    with pytest.raises(TypeError):
        MatchbookRateGovernor(
            "test:identity-direct",
            reports_policy(),
            clock=clock,
        )


def test_invalid_throttle_evidence_never_increases_allowance() -> None:
    governor, _ = ready_reports(
        "test:invalid-throttle",
        capacity=2,
        reserve=0,
    )
    governor.admit(MatchbookRateBucket.REPORTS)

    for invalid in (-1.0, math.nan, math.inf, True):
        with pytest.raises(MatchbookRateGovernorError):
            governor.observe_throttle(
                MatchbookRateBucket.REPORTS,
                retry_after_seconds=invalid,
            )

    governor.admit(MatchbookRateBucket.REPORTS)
    with pytest.raises(MatchbookRateDeferred):
        governor.admit(MatchbookRateBucket.REPORTS)


def test_monotonic_clock_rollback_fails_closed_permanently() -> None:
    governor, clock = ready_reports(
        "test:clock-rollback",
        capacity=3,
        reserve=0,
    )
    governor.admit(MatchbookRateBucket.REPORTS)

    clock.value -= 1.0
    with pytest.raises(MatchbookRateDeferred) as first:
        governor.admit(MatchbookRateBucket.REPORTS)
    assert first.value.reason == "monotonic_clock_rollback"

    clock.value += 100.0
    with pytest.raises(MatchbookRateDeferred) as second:
        governor.admit(MatchbookRateBucket.REPORTS)
    assert second.value.reason == "monotonic_clock_failed_closed"
    assert second.value.retry_after_seconds is None


def test_success_cannot_clear_unexpired_throttle_or_sibling_bucket() -> None:
    clock = FakeClock()
    policies = {
        MatchbookRateBucket.EVENTS: MatchbookRatePolicy(2, 60.0),
        MatchbookRateBucket.REPORTS: MatchbookRatePolicy(2, 60.0),
    }
    governor = resolve_matchbook_rate_governor(
        "test:success-boundary",
        policies,
        clock=clock,
    )
    clock.advance(600.0)
    governor.observe_throttle(
        MatchbookRateBucket.REPORTS,
        retry_after_seconds=6.0,
    )

    snapshot = governor.observe_success(MatchbookRateBucket.REPORTS)
    assert snapshot.blocked_for_seconds == pytest.approx(600.0)

    event_receipt = governor.admit(MatchbookRateBucket.EVENTS)
    assert event_receipt.bucket is MatchbookRateBucket.EVENTS

    clock.advance(600.0)
    restored = governor.observe_success(MatchbookRateBucket.REPORTS)
    assert restored.blocked_for_seconds == 0.0
    governor.admit(MatchbookRateBucket.REPORTS)


def test_documented_provider_envelope_cannot_be_relaxed_by_configuration() -> None:
    clock = FakeClock()
    unsafe = (
        (MatchbookRateBucket.ACCOUNT, 301, 60.0),
        (MatchbookRateBucket.EVENTS, 701, 60.0),
        (MatchbookRateBucket.SECURITY, 201, 60.0),
        (MatchbookRateBucket.NAVIGATION, 101, 60.0),
        (MatchbookRateBucket.REPORTS, 41, 60.0),
        (MatchbookRateBucket.REPORTS, 40, 59.999),
    )

    for index, (bucket, capacity, window) in enumerate(unsafe):
        with pytest.raises(MatchbookRateGovernorError, match="documented"):
            resolve_matchbook_rate_governor(
                f"test:unsafe-provider-envelope:{index}",
                {
                    bucket: MatchbookRatePolicy(
                        capacity=capacity,
                        window_seconds=window,
                    )
                },
                clock=clock,
            )


def test_conservative_policy_below_provider_envelope_is_allowed() -> None:
    clock = FakeClock()
    governor = resolve_matchbook_rate_governor(
        "test:conservative-provider-envelope",
        {
            MatchbookRateBucket.REPORTS: MatchbookRatePolicy(
                capacity=20,
                window_seconds=120.0,
                safety_read_reserve=1,
            )
        },
        clock=clock,
    )
    clock.advance(600.0)

    for _ in range(19):
        governor.admit(MatchbookRateBucket.REPORTS)
    with pytest.raises(MatchbookRateDeferred) as exc_info:
        governor.admit(MatchbookRateBucket.REPORTS)
    assert exc_info.value.reason == "safety_read_capacity_reserved"

    governor.admit(
        MatchbookRateBucket.REPORTS,
        priority=MatchbookRatePriority.SAFETY_READ,
    )
    with pytest.raises(MatchbookRateDeferred):
        governor.admit(
            MatchbookRateBucket.REPORTS,
            priority=MatchbookRatePriority.SAFETY_READ,
        )


def test_cold_start_cannot_undercut_documented_default_provider_block() -> None:
    with pytest.raises(
        MatchbookRateGovernorError,
        match="documented default provider blocking period",
    ):
        MatchbookRatePolicy(
            capacity=40,
            window_seconds=60.0,
            cold_start_seconds=599.999,
        )

    policy = MatchbookRatePolicy(
        capacity=40,
        window_seconds=60.0,
        cold_start_seconds=600.0,
    )
    assert policy.cold_start_seconds == 600.0


def test_throttle_uses_documented_block_floor_and_honors_longer_provider_delay() -> None:
    governor, clock = ready_reports("test:throttle-floor")

    governor.observe_throttle(
        MatchbookRateBucket.REPORTS,
        retry_after_seconds=0.0,
    )
    with pytest.raises(MatchbookRateDeferred) as default_block:
        governor.admit(MatchbookRateBucket.REPORTS)
    assert default_block.value.retry_after_seconds == pytest.approx(600.0)

    clock.advance(600.0)
    governor.observe_throttle(
        MatchbookRateBucket.REPORTS,
        retry_after_seconds=900.0,
    )
    with pytest.raises(MatchbookRateDeferred) as longer_block:
        governor.admit(MatchbookRateBucket.REPORTS)
    assert longer_block.value.retry_after_seconds == pytest.approx(900.0)


def test_betting_write_budget_is_not_supported_by_read_governor() -> None:
    with pytest.raises(
        MatchbookRateGovernorError,
        match="Betting Write budget",
    ):
        resolve_matchbook_rate_governor(
            "test:no-write-budget",
            {
                MatchbookRateBucket.BETTING_WRITE: MatchbookRatePolicy(
                    1,
                    60.0,
                )
            },
            clock=FakeClock(),
        )


def test_process_boundary_is_explicit_and_no_admission_grants_execution() -> None:
    governor, _ = ready_reports("test:process-boundary")
    assert governor.multi_process_safe is False
    snapshot = governor.snapshot(MatchbookRateBucket.REPORTS)
    assert snapshot.multi_process_safe is False

    admission = governor.admit(
        MatchbookRateBucket.REPORTS,
        priority=MatchbookRatePriority.SAFETY_READ,
    )
    assert admission.grants_execution_authority is False


def test_same_account_different_networks_share_account_budget() -> None:
    clock = FakeClock()
    policy = reports_policy(capacity=2, reserve=0)
    first = resolve_matchbook_rate_governor(
        "dual:account:shared",
        policy,
        network_scope_id="dual:network:a",
        clock=clock,
    )
    second = resolve_matchbook_rate_governor(
        "dual:account:shared",
        policy,
        network_scope_id="dual:network:b",
        clock=clock,
    )
    assert first is not second
    clock.advance(600.0)

    first.admit(MatchbookRateBucket.REPORTS)
    second.admit(MatchbookRateBucket.REPORTS)
    with pytest.raises(MatchbookRateDeferred) as exc_info:
        first.admit(MatchbookRateBucket.REPORTS)
    assert exc_info.value.reason == "provider_rate_capacity_exhausted"


def test_same_network_different_accounts_share_network_budget() -> None:
    clock = FakeClock()
    policy = reports_policy(capacity=2, reserve=0)
    first = resolve_matchbook_rate_governor(
        "dual:account:a",
        policy,
        network_scope_id="dual:network:shared",
        clock=clock,
    )
    second = resolve_matchbook_rate_governor(
        "dual:account:b",
        policy,
        network_scope_id="dual:network:shared",
        clock=clock,
    )
    assert first is not second
    clock.advance(600.0)

    first.admit(MatchbookRateBucket.REPORTS)
    second.admit(MatchbookRateBucket.REPORTS)
    with pytest.raises(MatchbookRateDeferred) as exc_info:
        second.admit(MatchbookRateBucket.REPORTS)
    assert exc_info.value.reason == "provider_rate_capacity_exhausted"


def test_throttle_propagates_through_each_shared_scope_dimension() -> None:
    clock = FakeClock()
    policy = reports_policy(capacity=3, reserve=0)
    origin = resolve_matchbook_rate_governor(
        "dual:account:throttle",
        policy,
        network_scope_id="dual:network:throttle",
        clock=clock,
    )
    same_account = resolve_matchbook_rate_governor(
        "dual:account:throttle",
        policy,
        network_scope_id="dual:network:other",
        clock=clock,
    )
    same_network = resolve_matchbook_rate_governor(
        "dual:account:other",
        policy,
        network_scope_id="dual:network:throttle",
        clock=clock,
    )
    clock.advance(600.0)

    origin.observe_throttle(
        MatchbookRateBucket.REPORTS,
        retry_after_seconds=1.0,
    )
    for sibling in (same_account, same_network):
        with pytest.raises(MatchbookRateDeferred) as exc_info:
            sibling.admit(MatchbookRateBucket.REPORTS)
        assert exc_info.value.retry_after_seconds == pytest.approx(600.0)


def test_legacy_omitted_network_scope_cannot_multiply_budget_by_account_alias() -> None:
    clock = FakeClock()
    policy = reports_policy(capacity=2, reserve=0)
    first = resolve_matchbook_rate_governor(
        "legacy:account:a",
        policy,
        clock=clock,
    )
    second = resolve_matchbook_rate_governor(
        "legacy:account:b",
        policy,
        clock=clock,
    )
    assert first is not second
    clock.advance(600.0)

    first.admit(MatchbookRateBucket.REPORTS)
    second.admit(MatchbookRateBucket.REPORTS)
    with pytest.raises(MatchbookRateDeferred):
        first.admit(MatchbookRateBucket.REPORTS)


def test_failed_cross_dimension_resolve_does_not_poison_new_scope() -> None:
    clock = FakeClock()
    resolve_matchbook_rate_governor(
        "atomic:account:existing",
        reports_policy(capacity=40),
        network_scope_id="atomic:network:conflict",
        clock=clock,
    )

    with pytest.raises(
        MatchbookRateGovernorError,
        match="network scope.*different rate policy",
    ):
        resolve_matchbook_rate_governor(
            "atomic:account:must-remain-unbound",
            reports_policy(capacity=39),
            network_scope_id="atomic:network:conflict",
            clock=clock,
        )

    governor = resolve_matchbook_rate_governor(
        "atomic:account:must-remain-unbound",
        reports_policy(capacity=40),
        network_scope_id="atomic:network:clean",
        clock=clock,
    )
    assert governor.account_scope_id == "atomic:account:must-remain-unbound"
    assert governor.network_scope_id == "atomic:network:clean"
