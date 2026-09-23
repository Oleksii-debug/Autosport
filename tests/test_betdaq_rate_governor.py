import pytest

from autosport import betdaq_rate_governor as brg


class Clock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = float(value)

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def setup_function() -> None:
    brg.reset_betdaq_rate_governor_registry_for_tests()


def governor(
    *,
    method_capacity: dict[brg.BetdaqApiMethod, int] | None = None,
    combined: int = 300,
    safety: int = 0,
    reconciliation: int = 0,
) -> tuple[brg.BetdaqRateGovernor, Clock, Clock]:
    clock = Clock(1000)
    utc = Clock(2_000_000_000)
    capacities = method_capacity or {brg.BetdaqApiMethod.GET_PRICES: 130}
    policy = brg.BetdaqRatePolicy(
        method_capacity=capacities,
        combined_capacity=combined,
        safety_reserve=safety,
        reconciliation_reserve=reconciliation,
        cold_start_seconds=60,
        revision="test",
    )
    resolved = brg.resolve_betdaq_rate_governor(
        "acct-safe",
        policy=policy,
        clock=clock,
        utc_clock=utc,
    )
    clock.advance(60)
    return resolved, clock, utc


def test_getprices_131st_is_denied() -> None:
    resolved, _, _ = governor()
    for _ in range(130):
        resolved.admit(brg.BetdaqApiMethod.GET_PRICES)
    with pytest.raises(brg.BetdaqRateDeferred) as exc:
        resolved.admit(brg.BetdaqApiMethod.GET_PRICES)
    assert exc.value.reason == "method_budget_exhausted"


def test_combined_budget_and_reserves() -> None:
    resolved, _, _ = governor(
        method_capacity={
            brg.BetdaqApiMethod.GET_PRICES: 10,
            brg.BetdaqApiMethod.LIST_BOOTSTRAP_ORDERS: 10,
        },
        combined=6,
        safety=1,
        reconciliation=2,
    )
    for _ in range(3):
        resolved.admit(brg.BetdaqApiMethod.GET_PRICES)
    with pytest.raises(brg.BetdaqRateDeferred) as exc:
        resolved.admit(brg.BetdaqApiMethod.GET_PRICES)
    assert exc.value.reason == "combined_background_reserve"

    resolved.admit(
        brg.BetdaqApiMethod.LIST_BOOTSTRAP_ORDERS,
        priority=brg.BetdaqRatePriority.RECONCILIATION,
    )
    resolved.admit(
        brg.BetdaqApiMethod.LIST_BOOTSTRAP_ORDERS,
        priority=brg.BetdaqRatePriority.RECONCILIATION,
    )
    with pytest.raises(brg.BetdaqRateDeferred) as exc:
        resolved.admit(
            brg.BetdaqApiMethod.LIST_BOOTSTRAP_ORDERS,
            priority=brg.BetdaqRatePriority.RECONCILIATION,
        )
    assert exc.value.reason == "combined_safety_reserve"

    resolved.admit(
        brg.BetdaqApiMethod.LIST_BOOTSTRAP_ORDERS,
        priority=brg.BetdaqRatePriority.SAFETY,
    )


def test_documented_default_cannot_be_widened() -> None:
    with pytest.raises(brg.BetdaqRateGovernorError):
        brg.BetdaqRatePolicy(
            method_capacity={brg.BetdaqApiMethod.GET_PRICES: 131},
            combined_capacity=300,
        )
    with pytest.raises(brg.BetdaqRateGovernorError):
        brg.BetdaqRatePolicy(
            method_capacity={brg.BetdaqApiMethod.GET_PRICES: 130},
            combined_capacity=301,
        )


def test_standard_is_not_caller_unlockable() -> None:
    resolved, _, _ = governor()
    assert resolved.tier is brg.BetdaqRateTier.DEFAULT
    assert not hasattr(resolved, "standard_api")
    assert (
        brg._DOCUMENTED_STANDARD_PER_MINUTE[
            brg.BetdaqApiMethod.GET_PRICES
        ]
        == 2000
    )
    assert resolved.policy.method_capacity[brg.BetdaqApiMethod.GET_PRICES] == 130


def test_blacklist_is_api_scoped() -> None:
    resolved, clock, _ = governor(
        method_capacity={
            brg.BetdaqApiMethod.GET_PRICES: 10,
            brg.BetdaqApiMethod.LIST_BOOTSTRAP_ORDERS: 10,
        }
    )
    resolved.observe_blacklist(
        brg._issue_verified_blacklist_evidence(
            api_method=brg.BetdaqApiMethod.GET_PRICES,
            remaining_ms=60_000,
            observation_id="obs-1",
        )
    )
    with pytest.raises(brg.BetdaqRateDeferred) as exc:
        resolved.admit(
            brg.BetdaqApiMethod.GET_PRICES,
            priority=brg.BetdaqRatePriority.SAFETY,
        )
    assert exc.value.reason == "provider_api_blacklisted"

    resolved.admit(
        brg.BetdaqApiMethod.LIST_BOOTSTRAP_ORDERS,
        priority=brg.BetdaqRatePriority.SAFETY,
    )
    clock.advance(60)
    resolved.admit(
        brg.BetdaqApiMethod.GET_PRICES,
        priority=brg.BetdaqRatePriority.SAFETY,
    )


def test_unverified_blacklist_cannot_change_fence() -> None:
    resolved, _, _ = governor()
    evidence = brg.BetdaqBlacklistEvidence(
        provider="BETDAQ",
        api_method=brg.BetdaqApiMethod.GET_PRICES,
        remaining_ms=60_000,
        observation_id="obs-x",
    )
    with pytest.raises(brg.BetdaqRateGovernorError):
        resolved.observe_blacklist(evidence)


def test_canonical_scope_shares_budget() -> None:
    clock = Clock(10)
    utc = Clock(100)
    policy = brg.BetdaqRatePolicy(
        method_capacity={brg.BetdaqApiMethod.GET_PRICES: 1},
        combined_capacity=1,
        cold_start_seconds=60,
    )
    first = brg.resolve_betdaq_rate_governor(
        "same",
        policy=policy,
        clock=clock,
        utc_clock=utc,
    )
    second = brg.resolve_betdaq_rate_governor(
        "same",
        policy=policy,
        clock=clock,
        utc_clock=utc,
    )
    assert first is second
    clock.advance(60)
    first.admit(
        brg.BetdaqApiMethod.GET_PRICES,
        priority=brg.BetdaqRatePriority.SAFETY,
    )
    with pytest.raises(brg.BetdaqRateDeferred):
        second.admit(
            brg.BetdaqApiMethod.GET_PRICES,
            priority=brg.BetdaqRatePriority.SAFETY,
        )


def test_restart_cold_start_fence_and_no_authority_flags() -> None:
    clock = Clock(50)
    utc = Clock(500)
    policy = brg.BetdaqRatePolicy(
        method_capacity={brg.BetdaqApiMethod.GET_PRICES: 1},
        combined_capacity=1,
        cold_start_seconds=60,
    )
    resolved = brg.resolve_betdaq_rate_governor(
        "cold",
        policy=policy,
        clock=clock,
        utc_clock=utc,
    )
    with pytest.raises(brg.BetdaqRateDeferred) as exc:
        resolved.admit(brg.BetdaqApiMethod.GET_PRICES)
    assert exc.value.reason == "restart_cold_start_fence"

    clock.advance(60)
    receipt = resolved.admit(
        brg.BetdaqApiMethod.GET_PRICES,
        priority=brg.BetdaqRatePriority.SAFETY,
    )
    assert not receipt.grants_execution_authority
    assert not receipt.grants_write_permission
    assert not receipt.grants_freshness
    assert not receipt.multi_process_safe
    assert len(receipt.receipt_sha256) == 64


def test_monotonic_rollback_fails_closed() -> None:
    resolved, clock, _ = governor()
    resolved.admit(
        brg.BetdaqApiMethod.GET_PRICES,
        priority=brg.BetdaqRatePriority.SAFETY,
    )
    clock.value -= 1
    with pytest.raises(brg.BetdaqRateDeferred) as exc:
        resolved.admit(brg.BetdaqApiMethod.GET_PRICES)
    assert exc.value.reason == "monotonic_clock_rollback"

    clock.value += 2
    with pytest.raises(brg.BetdaqRateDeferred) as exc:
        resolved.admit(brg.BetdaqApiMethod.GET_PRICES)
    assert exc.value.reason == "monotonic_clock_failed_closed"


def test_unknown_method_policy_denies_and_no_any_inference() -> None:
    resolved, _, _ = governor()
    with pytest.raises(brg.BetdaqRateDeferred) as exc:
        resolved.admit(brg.BetdaqApiMethod.LIST_BLACKLIST_INFORMATION)
    assert exc.value.reason == "method_policy_unknown"
