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


def gate() -> BetfairMarketBookProjectionConcurrencyGate:
    return BetfairMarketBookProjectionConcurrencyGate()


def begin_projected(
    value: BetfairMarketBookProjectionConcurrencyGate,
    request_id: str,
    *,
    at: datetime = T0,
):
    return value.begin(
        request_id,
        observed_at=at,
        has_order_projection=True,
        has_match_projection=False,
    )


def test_first_three_projection_requests_are_allowed_and_fourth_is_denied() -> None:
    value = gate()
    for index in range(3):
        decision = begin_projected(value, f"r{index}")
        assert decision.allowed is True
        assert decision.active_projection_requests == index + 1

    denied = begin_projected(value, "r3")
    assert denied.allowed is False
    assert denied.active_projection_requests == 3
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


def test_price_only_request_bypasses_projection_bucket_even_when_full() -> None:
    value = gate()
    for index in range(3):
        assert begin_projected(value, f"projected-{index}").allowed

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
        assert begin_projected(value, f"r{index}").allowed

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
    assert begin_projected(value, "r0").allowed
    value.complete("r0", observed_at=T0 + timedelta(seconds=1))
    with pytest.raises(ValueError, match="not an active"):
        value.complete("r0", observed_at=T0 + timedelta(seconds=1))
    assert value.snapshot().active == ()


def test_duplicate_active_request_id_is_rejected_without_extra_slot() -> None:
    value = gate()
    assert begin_projected(value, "same").allowed
    with pytest.raises(ValueError, match="already active"):
        value.begin(
            "same",
            observed_at=T0,
            has_order_projection=False,
            has_match_projection=True,
        )
    assert len(value.snapshot().active) == 1


def test_time_advance_never_auto_expires_unresolved_leases() -> None:
    value = gate()
    for index in range(3):
        assert begin_projected(value, f"r{index}").allowed

    denied = begin_projected(value, "r3", at=T0 + timedelta(days=365))
    assert denied.allowed is False
    assert denied.active_projection_requests == 3
    assert [item.request_id for item in value.snapshot().active] == [
        "r0",
        "r1",
        "r2",
    ]


def test_explicit_completion_can_release_old_unresolved_lease() -> None:
    value = gate()
    assert begin_projected(value, "r0").allowed
    value.complete("r0", observed_at=T0 + timedelta(days=365))
    assert value.snapshot().active == ()


def test_restart_preserves_unresolved_slots_and_denial() -> None:
    value = gate()
    for index in range(3):
        assert begin_projected(
            value,
            f"r{index}",
            at=T0 + timedelta(seconds=index),
        ).allowed
    restored = BetfairMarketBookProjectionConcurrencyGate(state=value.snapshot())
    when = T0 + timedelta(days=1)
    original = begin_projected(value, "r3", at=when)
    replay = begin_projected(restored, "r3", at=when)
    assert original == replay
    assert original.allowed is False
    assert value.snapshot() == restored.snapshot()


def test_observed_time_cannot_move_backwards() -> None:
    value = gate()
    assert begin_projected(value, "r0", at=T0 + timedelta(seconds=1)).allowed
    with pytest.raises(ValueError, match="must not move backwards"):
        begin_projected(value, "r1", at=T0)


def test_timezone_equivalent_instants_normalize_identically() -> None:
    value = gate()
    plus_two = timezone(timedelta(hours=2))
    first = begin_projected(value, "r0")
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


def test_state_rejects_wrong_policy_version() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        MarketBookProjectionConcurrencyState("wrong", None, ())


def test_state_rejects_more_than_three_active_projection_requests() -> None:
    leases = tuple(
        MarketBookProjectionLease(f"r{index}", 0)
        for index in range(4)
    )
    with pytest.raises(ValueError, match="exceeds"):
        MarketBookProjectionConcurrencyState(
            BETFAIR_MARKETBOOK_PROJECTION_CONCURRENCY_POLICY_VERSION,
            0,
            leases,
        )


def test_state_rejects_unsorted_or_duplicate_active_ids() -> None:
    a = MarketBookProjectionLease("a", 0)
    b = MarketBookProjectionLease("b", 0)
    with pytest.raises(ValueError, match="sorted"):
        MarketBookProjectionConcurrencyState(
            BETFAIR_MARKETBOOK_PROJECTION_CONCURRENCY_POLICY_VERSION,
            0,
            (b, a),
        )
    with pytest.raises(ValueError, match="duplicate"):
        MarketBookProjectionConcurrencyState(
            BETFAIR_MARKETBOOK_PROJECTION_CONCURRENCY_POLICY_VERSION,
            0,
            (a, a),
        )


def test_state_rejects_lease_acquired_after_last_observed_time() -> None:
    lease = MarketBookProjectionLease("a", 2)
    with pytest.raises(ValueError, match="after last"):
        MarketBookProjectionConcurrencyState(
            BETFAIR_MARKETBOOK_PROJECTION_CONCURRENCY_POLICY_VERSION,
            1,
            (lease,),
        )


def test_denial_adds_no_lease() -> None:
    value = gate()
    for index in range(3):
        assert begin_projected(value, f"r{index}").allowed
    before = value.snapshot().active
    assert not begin_projected(
        value,
        "denied",
        at=T0 + timedelta(seconds=1),
    ).allowed
    assert value.snapshot().active == before


def test_price_only_request_never_needs_completion() -> None:
    value = gate()
    decision = value.begin(
        "price",
        observed_at=T0,
        has_order_projection=False,
        has_match_projection=False,
    )
    assert decision.allowed
    assert value.snapshot().active == ()
    with pytest.raises(ValueError, match="not an active"):
        value.complete("price", observed_at=T0)
