from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from autosport.betfair_marketbook_projection_concurrency import (
    BETFAIR_MARKETBOOK_PROJECTION_CONCURRENCY_POLICY_VERSION,
    BetfairMarketBookProjectionConcurrencyGate,
    MarketBookProjectionConcurrencyState,
    MarketBookProjectionLease,
)


T0 = datetime(2026, 9, 22, tzinfo=timezone.utc)
HOLD = timedelta(seconds=30)


def gate() -> BetfairMarketBookProjectionConcurrencyGate:
    return BetfairMarketBookProjectionConcurrencyGate(max_inflight_hold=HOLD)


def test_first_three_projection_requests_are_allowed_and_fourth_is_denied() -> None:
    value = gate()
    for index in range(3):
        decision = value.begin(
            f"r{index}",
            observed_at=T0,
            has_order_projection=True,
            has_match_projection=False,
        )
        assert decision.allowed is True
        assert decision.active_projection_requests == index + 1

    denied = value.begin(
        "r3",
        observed_at=T0,
        has_order_projection=True,
        has_match_projection=False,
    )
    assert denied.allowed is False
    assert denied.active_projection_requests == 3
    assert denied.next_eligible_at == T0 + HOLD
    assert [item.request_id for item in value.snapshot().active] == [
        "r0",
        "r1",
        "r2",
    ]


@pytest.mark.parametrize(
    ("order", "match"),
    [(True, False), (False, True), (True, True)],
)
def test_any_order_or_match_projection_consumes_bucket(
    order: bool,
    match: bool,
) -> None:
    value = gate()
    decision = value.begin(
        "r",
        observed_at=T0,
        has_order_projection=order,
        has_match_projection=match,
    )
    assert decision.projection_bearing is True
    assert len(value.snapshot().active) == 1


def test_price_only_request_bypasses_projection_concurrency_bucket() -> None:
    value = gate()
    for index in range(3):
        assert value.begin(
            f"projected-{index}",
            observed_at=T0,
            has_order_projection=True,
            has_match_projection=False,
        ).allowed

    for index in range(20):
        decision = value.begin(
            f"price-{index}",
            observed_at=T0,
            has_order_projection=False,
            has_match_projection=False,
        )
        assert decision.allowed is True
        assert decision.projection_bearing is False
        assert decision.active_projection_requests == 3
    assert len(value.snapshot().active) == 3


def test_completion_releases_exactly_one_slot() -> None:
    value = gate()
    for index in range(3):
        assert value.begin(
            f"r{index}",
            observed_at=T0,
            has_order_projection=True,
            has_match_projection=False,
        ).allowed

    value.complete("r1", observed_at=T0 + timedelta(seconds=1))
    assert [item.request_id for item in value.snapshot().active] == ["r0", "r2"]
    replacement = value.begin(
        "r3",
        observed_at=T0 + timedelta(seconds=1),
        has_order_projection=False,
        has_match_projection=True,
    )
    assert replacement.allowed is True
    assert replacement.active_projection_requests == 3


def test_duplicate_completion_cannot_double_release() -> None:
    value = gate()
    assert value.begin(
        "r0",
        observed_at=T0,
        has_order_projection=True,
        has_match_projection=False,
    ).allowed
    value.complete("r0", observed_at=T0 + timedelta(seconds=1))
    with pytest.raises(ValueError, match="not an active"):
        value.complete("r0", observed_at=T0 + timedelta(seconds=1))
    assert value.snapshot().active == ()


def test_duplicate_active_request_id_is_rejected_without_extra_slot() -> None:
    value = gate()
    assert value.begin(
        "same",
        observed_at=T0,
        has_order_projection=True,
        has_match_projection=False,
    ).allowed
    with pytest.raises(ValueError, match="already active"):
        value.begin(
            "same",
            observed_at=T0,
            has_order_projection=False,
            has_match_projection=True,
        )
    assert len(value.snapshot().active) == 1


def test_expired_lease_is_conservatively_released() -> None:
    value = gate()
    for index in range(3):
        assert value.begin(
            f"r{index}",
            observed_at=T0,
            has_order_projection=True,
            has_match_projection=False,
        ).allowed
    decision = value.begin(
        "r3",
        observed_at=T0 + HOLD,
        has_order_projection=True,
        has_match_projection=False,
    )
    assert decision.allowed is True
    assert decision.active_projection_requests == 1
    assert [item.request_id for item in value.snapshot().active] == ["r3"]


def test_next_eligible_uses_earliest_hold_expiry() -> None:
    value = gate()
    for index, offset in enumerate((0, 1, 2)):
        assert value.begin(
            f"r{index}",
            observed_at=T0 + timedelta(seconds=offset),
            has_order_projection=True,
            has_match_projection=False,
        ).allowed
    denied = value.begin(
        "r3",
        observed_at=T0 + timedelta(seconds=3),
        has_order_projection=True,
        has_match_projection=False,
    )
    assert denied.allowed is False
    assert denied.next_eligible_at == T0 + HOLD


def test_restart_preserves_active_slots_and_decision() -> None:
    value = gate()
    for index in range(3):
        assert value.begin(
            f"r{index}",
            observed_at=T0 + timedelta(seconds=index),
            has_order_projection=True,
            has_match_projection=False,
        ).allowed
    restored = BetfairMarketBookProjectionConcurrencyGate(
        max_inflight_hold=HOLD,
        state=value.snapshot(),
    )
    when = T0 + timedelta(seconds=3)
    original = value.begin(
        "r3",
        observed_at=when,
        has_order_projection=True,
        has_match_projection=False,
    )
    replay = restored.begin(
        "r3",
        observed_at=when,
        has_order_projection=True,
        has_match_projection=False,
    )
    assert original == replay
    assert value.snapshot() == restored.snapshot()


def test_restart_rejects_different_hold_policy() -> None:
    value = gate()
    state = value.snapshot()
    with pytest.raises(ValueError, match="does not match"):
        BetfairMarketBookProjectionConcurrencyGate(
            max_inflight_hold=timedelta(seconds=10),
            state=state,
        )


def test_observed_time_cannot_move_backwards() -> None:
    value = gate()
    assert value.begin(
        "r0",
        observed_at=T0 + timedelta(seconds=1),
        has_order_projection=True,
        has_match_projection=False,
    ).allowed
    with pytest.raises(ValueError, match="must not move backwards"):
        value.begin(
            "r1",
            observed_at=T0,
            has_order_projection=True,
            has_match_projection=False,
        )


def test_timezone_equivalent_instants_normalize_identically() -> None:
    value = gate()
    plus_two = timezone(timedelta(hours=2))
    first = value.begin(
        "r0",
        observed_at=T0,
        has_order_projection=True,
        has_match_projection=False,
    )
    second = value.begin(
        "price",
        observed_at=T0.astimezone(plus_two),
        has_order_projection=False,
        has_match_projection=False,
    )
    assert first.observed_at_utc_us == second.observed_at_utc_us


@pytest.mark.parametrize("bad", ["", " x", "x ", 1, None])
def test_bad_request_id_fails_closed(bad: object) -> None:
    value = gate()
    with pytest.raises((TypeError, ValueError)):
        value.begin(
            bad,  # type: ignore[arg-type]
            observed_at=T0,
            has_order_projection=True,
            has_match_projection=False,
        )


@pytest.mark.parametrize(
    "name",
    ["has_order_projection", "has_match_projection"],
)
def test_projection_flags_require_exact_bool(name: str) -> None:
    value = gate()
    kwargs = {
        "request_id": "r",
        "observed_at": T0,
        "has_order_projection": False,
        "has_match_projection": False,
    }
    kwargs[name] = 1
    with pytest.raises(TypeError, match="must be bool"):
        value.begin(**kwargs)  # type: ignore[arg-type]


def test_naive_time_fails_closed() -> None:
    value = gate()
    with pytest.raises(ValueError, match="timezone-aware"):
        value.begin(
            "r",
            observed_at=datetime(2026, 9, 22),
            has_order_projection=True,
            has_match_projection=False,
        )


@pytest.mark.parametrize(
    "bad",
    [timedelta(0), timedelta(microseconds=-1), 1, None],
)
def test_invalid_hold_policy_fails_closed(bad: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        BetfairMarketBookProjectionConcurrencyGate(
            max_inflight_hold=bad  # type: ignore[arg-type]
        )


def test_state_rejects_more_than_three_active_projection_requests() -> None:
    leases = tuple(
        MarketBookProjectionLease(f"r{index}", 0, 30_000_000)
        for index in range(4)
    )
    with pytest.raises(ValueError, match="exceeds"):
        MarketBookProjectionConcurrencyState(
            BETFAIR_MARKETBOOK_PROJECTION_CONCURRENCY_POLICY_VERSION,
            30_000_000,
            0,
            leases,
        )


def test_state_rejects_unsorted_or_duplicate_active_ids() -> None:
    a = MarketBookProjectionLease("a", 0, 30_000_000)
    b = MarketBookProjectionLease("b", 0, 30_000_000)
    with pytest.raises(ValueError, match="sorted"):
        MarketBookProjectionConcurrencyState(
            BETFAIR_MARKETBOOK_PROJECTION_CONCURRENCY_POLICY_VERSION,
            30_000_000,
            0,
            (b, a),
        )
    with pytest.raises(ValueError, match="duplicate"):
        MarketBookProjectionConcurrencyState(
            BETFAIR_MARKETBOOK_PROJECTION_CONCURRENCY_POLICY_VERSION,
            30_000_000,
            0,
            (a, a),
        )


def test_state_rejects_lease_policy_mismatch() -> None:
    lease = MarketBookProjectionLease("a", 0, 10)
    with pytest.raises(ValueError, match="does not match"):
        MarketBookProjectionConcurrencyState(
            BETFAIR_MARKETBOOK_PROJECTION_CONCURRENCY_POLICY_VERSION,
            20,
            0,
            (lease,),
        )


def test_denial_adds_no_lease() -> None:
    value = gate()
    for index in range(3):
        assert value.begin(
            f"r{index}",
            observed_at=T0,
            has_order_projection=True,
            has_match_projection=False,
        ).allowed
    before = value.snapshot().active
    assert not value.begin(
        "denied",
        observed_at=T0 + timedelta(seconds=1),
        has_order_projection=True,
        has_match_projection=False,
    ).allowed
    assert value.snapshot().active == before


def test_completion_after_lease_expiry_fails_closed_as_second_release() -> None:
    value = gate()
    assert value.begin(
        "r0",
        observed_at=T0,
        has_order_projection=True,
        has_match_projection=False,
    ).allowed
    with pytest.raises(ValueError, match="not an active"):
        value.complete("r0", observed_at=T0 + HOLD)
    assert value.snapshot().active == ()
